# Routing

Everything on this page describes `tamias.router.decide`: a pure function that
looks at one request and answers STAY (keep the requested model) or SWITCH
(rewrite to a model the routing gates admit). It never mutates the request body
and never stores conversation text in a decision reason.

## Modes

`--router-mode` decides what becomes of a decision:

| Mode | What it does |
| --- | --- |
| `off` | Calls nothing. Every request runs on the model the caller named, and no decision is recorded. |
| `shadow` (default) | Calls `decide`, records the decision on the log row, and forwards the request byte-for-byte. A decision forced by availability (the requested model is locked out) still switches. |
| `active` | Calls `decide`, and when it says SWITCH rewrites `model` to a pool member before forwarding. |

Active is opt-in and **fail-closed**. `tamias serve` refuses to start active
unless the routing gates admit at least one target that is (1) in `--registry`,
(2) marked `tools = true` there, (3) priced by the price sheet, and (4) served by
a configured provider -- because the gateway prices, caps and reports the
request against the model that actually runs, not the one the caller typed. A
provider catalogue makes the pool dynamic, so this is checked over the *gated
universe* at start-up rather than against a hand-typed list; an explicit
`--cheap-model` list stays an allowlist and every one of its members is checked.

`tamias.router.applied_target` is the single place that turns a decision plus
those conditions into the model to run. It returns the target only when the
mode is active, or the decision was forced by availability, the target differs
from the requested model, and the caller's key (if any) allows it; otherwise it
returns `None`, meaning "no rewrite". A switch the caller's key does not allow
is **suppressed**: the request stays on the model it named rather than failing,
and the log still records the SWITCH that was declined.

Active routing rewrites `POST /v1/chat/completions` and `POST /v1/messages`.
`POST /v1/responses` is metered only and is never rewritten. Both rewritten
routes build their gate inputs from one helper, so the same agent step gets the
same decision whatever wire format carried it; an Anthropic `tool_result` turn
is normalized to the tool-turn shape the rules read.

## Rules, in order

0. The requested model is locked out by the limit ledger -> SWITCH to the most
   capable model that passed the gates, preferring one at least as good as the
   model asked for. Running it again can only fail, so this rule outranks every
   other and applies in every mode.
1. A trailing `user` message means the agent is planning -> STAY.
2. A trailing `tool` message containing an error marker (see
   [Error markers](#error-markers)) -> STAY.
3. A trailing `tool` message from a shell-family tool whose output is at or
   over `big_output_chars` -> STAY.
4. A trailing `tool` message from an easy tool, with at least `min_gap`
   requests since the last switch -> SWITCH to a model from the pool.
5. Anything else -> STAY.

While the session is already on a pool model the gap counts as zero, so
hysteresis keeps it there until `min_gap` fresh requests have passed.

## The model pool

The pool is whatever the three gates admit for this request -- capability,
supply, health. See [PROVIDERS.md](PROVIDERS.md) for what each gate checks and
how models are ranked. Two ways to bound it:

- **`--cheap-model`** takes one model or a comma-separated list and becomes an
  allowlist: routing may only choose from it.
- **A provider catalogue** (one or more `[[providers]]`) makes the pool dynamic:
  every discovered model that passes the gates is a candidate.

Within the pool:

- **A session stays on one member.** With an allowlist the member is chosen by a
  stable hash of the session id, so a conversation does not jump between models
  on every tool turn. With a gated pool the order is the ranking itself
  (cheapest first), so a newly cheaper or newly available model changes the
  answer without any state.
- **Different sessions spread across an allowlisted pool.** That spreads
  free-tier rate limits and load instead of sending every conversation to the
  same free model.
- **The requested model is skipped.** Switching to the model already running
  would be a no-op and could not save anything.
- **The first entry is the primary.** With one entry the behaviour is exactly
  the old single-target configuration.

The pool **is** the failover set. A retryable limit moves the same request to
the cheapest candidate that is at least as capable as the model that refused;
when every survivor is weaker, the request still runs on the strongest survivor
rather than failing. The limit ledger keeps the refused model out of later
decisions until its cooldown expires. A `400` or `422` is a request problem and
does not move anything. `active` still validates every member at start-up
(registry membership, tool support, a known price), so a pool cannot contain a
model the deployment has not vouched for.

A fresh session starts at zero requests since the last switch, so the default
`min_gap = 3` holds back the first three easy turns. `--min-gap 0` routes the
first one. The OpenCode setup in [OPENCODE.md](OPENCODE.md) uses `0` for that
reason; raise it if a long session flaps between models.

## The generic matcher

The matcher only ever looks at the **tool name**. It normalises the name --
lower-case, split on underscores, hyphens, dots and camelCase boundaries --
and checks the resulting tokens against three families:

| Family  | Tokens                                                                                        |
| ------- | --------------------------------------------------------------------------------------------- |
| read    | read, grep, glob, ls, list, find, cat, head, tail, search, view, stat                           |
| edit    | write, edit, patch, create, replace, multiedit, mkdir                                           |
| shell   | bash, shell, sh, run, exec, execute, command, terminal                                          |
| neutral | todo, plan, task, think (always UNKNOWN, e.g. `TodoWrite`, `update_plan`)                       |

Classification rules:

- a name carrying **any neutral token** is UNKNOWN;
- a name matching **two non-neutral families** is UNKNOWN (`read_shell` says
  nothing about what the agent meant);
- otherwise the single matched family wins; no match is UNKNOWN.

Each recognised family maps to exactly what v1 did with its legacy equivalent:

| Family | Legacy equivalent | Effect                                                                    |
| ------ | ----------------- | ------------------------------------------------------------------------- |
| read   | `read`            | easy: SWITCH after `min_gap`, subject to rules 1-3                        |
| shell  | `shell`           | easy, but only if the output carries no error marker and fits the size cap |
| edit   | `edit_file`       | never easy -> STAY (rule 5)                                               |
| unknown | (unrouted)       | never easy -> STAY (rule 5)                                               |

Names v1 classified keep their classification: the six names in `easy_tools`
still switch, and names v1 left unrouted (`edit_file`, `apply_patch`, ...) are
still left unrouted. `str_replace_editor` and friends are edit work; `Bash`,
`run_shell`, `read_file`, `list_directory`, `search_in_files` route like the
v1 tools they are.

## Profiles

`RouterConfig.profile` selects the matching strategy:

- **`generic`** (default): the token matcher above, plus the generic error
  markers and the shell size gate.
- **`legacy`**: exactly the v1 behaviour -- exact-name matching against
  `easy_tools` only, the v1 error markers, no size gate unless one is
  configured explicitly.

Any other value raises `ValueError` at construction time. All shared rules
(user turn, error markers, min-gap, hysteresis, edit tools staying put) hold
under both profiles.

## TOML configuration

`tamias.router_config.load_router_config(path)` reads a TOML file into a
`RouterConfig`. The file is parsed with `tomllib` and never evaluated, so a
config file cannot run code.

| Key                | Type                | Meaning                                                                |
| ------------------ | ------------------- | ---------------------------------------------------------------------- |
| `easy_tools`       | array of strings    | extra exact names that are easy (added to v1's six)                     |
| `edit_tools`       | array of strings    | extra exact names pinned to the edit family                             |
| `shell_tools`      | array of strings    | extra exact names pinned to the shell family                            |
| `error_markers`    | array of strings    | substrings that mark a tool result as failed                            |
| `big_output_chars` | positive integer    | cap on shell output length; at or over it -> STAY (unset = no cap)      |
| `min_gap`          | non-negative int    | requests required between switches (default 3)                          |

Example:

```toml
easy_tools = ["whisk"]
edit_tools = ["cat"]
shell_tools = ["gem"]
error_markers = ["Traceback", "FAILED", "error:", "Exception", "non-zero exit code"]
big_output_chars = 4096
min_gap = 7
```

Validation is strict: unknown keys (including `profile`, `cheap_model` and
`strong_model`) are rejected, each value must match its type (`bool` is not an
integer here), and every error names the file, the key and the problem. A
missing file raises `FileNotFoundError`; malformed TOML raises `ValueError`.

## Limits

- **Name-based, not semantic.** The matcher reads tool names only. It never
  inspects arguments, command lines or output content to decide *what* a tool
  did -- content is consulted only for error markers and the size cap. A tool
  named `read` that actually edits files still routes.
- **Error markers.** Rule 2 uses `RouterConfig.error_markers` for every tool.
  The defaults differ by profile:
  - `legacy`: `Traceback`, `FAILED`, `error:` -- exactly v1;
  - `generic`: those three plus `Exception` and `non-zero exit code`.
  Matching is case-sensitive substring search, as in v1. `Error:` (capital E)
  is **not** a default marker: `tests/test_router.py::test_error_markers_are_case_sensitive`
  pins it as not-a-marker for the default config, so it is opt-in via
  `error_markers` in TOML or `RouterConfig(error_markers=...)`.
- **Size cap.** `big_output_chars` applies only to shell-family results and
  only when configured; there is no built-in default.
- **Fixtures only.** Every routing test runs against fixed fixture
  conversations (no network, no live agents), and decisions are asserted on
  action, target and reason -- never on prompt text.

## Reasoning-effort switching

The effort axis is optional and independent of model switching. It adds
`reasoning.effort` (OpenRouter style) or `reasoning_effort` (OpenAI style) to
chat-completions request bodies, so the router can control how much effort the
upstream model spends on internal reasoning.

Request fields, per OpenRouter's parameter documentation:

- **Request field**: `"reasoning": {"effort": "low"|"medium"|"high"}`
  (<https://openrouter.ai/docs/parameters>, under "Reasoning").
- **OpenAI-style alternative**: `"reasoning_effort"`, enum `(xhigh, high,
  medium, low, minimal, none)` (same page, under "Reasoning Effort").
- **Model support**: determined by `supported_parameters` in the model list API.
  Models that support reasoning carry a `reasoning` config field with
  `supported_efforts` listing the allowed values (e.g. `["high", "medium",
  "low", "minimal"]`). See
  <https://openrouter.ai/docs/api/api-reference/models/list-all-models-and-their-properties>.

### CLI flags

- `--effort-mode {off,meter,apply}` (default `off`) — the scope of the effort
  axis, independent of `--router-mode`. `off` computes nothing; `meter` records
  the effort the policy would have used (the `decision_target_effort` column)
  without changing the request; `apply` also sends it. `apply` is gated by
  `--registry`: an effort is sent only for a model the registry lists as
  supporting reasoning, and a level the model does not list is downgraded to the
  nearest one it does.
- `--registry PATH` (default `$TAMIAS_REGISTRY`, else `~/.tamias/models.toml`) —
  the capability catalogue `apply` consults. The default path may be absent
  (empty registry, so nothing is applied); an explicitly named file must exist.
- `--effort-style {openrouter,openai}` (default `openrouter`) — controls whether
  the proxy sets `reasoning.effort` (OpenRouter) or `reasoning_effort` (OpenAI)
  in upstream requests.

Effort switching is off by default, and `apply` stays opt-in until a benchmark
measures it against a fixed-effort baseline; `meter` is the evidence-gathering
step on the way there.

### Caveats

- Providers may ignore the `reasoning.effort` or `reasoning_effort` field
  entirely.
- Effort changes may invalidate the prompt cache on some providers (not measured
  here).
- Effort switching has not been demonstrated end to end.
- The effort axis rewrites chat-completion request bodies only; it is **not**
  applied to the Anthropic Messages route or to `/v1/responses`.
