# Routing

Everything on this page describes `tamias.router.decide`: a pure function that
looks at one request and answers STAY (keep the requested model) or SWITCH
(rewrite to the configured cheap model). It never mutates the request body and
never stores conversation text in a decision reason.

## Rules, in order

1. A trailing `user` message means the agent is planning -> STAY.
2. A trailing `tool` message containing an error marker (see
   [Error markers](#error-markers)) -> STAY.
3. A trailing `tool` message from a shell-family tool whose output is at or
   over `big_output_chars` -> STAY.
4. A trailing `tool` message from an easy tool, with at least `min_gap`
   requests since the last switch -> SWITCH to `cheap_model`.
5. Anything else -> STAY.

While the session is already on the cheap model the gap counts as zero, so
hysteresis keeps it there until `min_gap` fresh requests have passed.

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
