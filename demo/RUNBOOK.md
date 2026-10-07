# Demo runbook

Everything below is run from the repo root on the `feat/demo` branch. Every
command uses the project venv; there is no bare `python` or `pytest` anywhere in
this document.

The demo is one small task run twice under two arms:

| arm | proxy flags | what it measures |
| --- | --- | --- |
| `baseline` | `--router-mode shadow` | what the router **would** do; every request still runs the strong model |
| `routed` | `--router-mode active --cheap-model … --strong-model … --inject-usage` | what it **did**: mechanical tool work is rewritten to the cheap model |

Each run writes `demo/runs/<arm>-<n>.db` (the request log) and
`demo/runs/<arm>-<n>.json` (the score). The runs from 2026-10-05 are committed
as the evidence this document reads from. `.gitignore` still ignores `*.db` and
`demo/runs/`, so a run you produce yourself stays out of a commit unless you
`git add -f` it deliberately.

---

## 0. Rehearse first, with no key and no network

Do this before anything else. It starts the real proxy against the test suite's
mock upstream on port 9001, sends 12 realistic requests, and costs nothing.

```sh
bash demo/run_arm.sh routed 1 --dry-run
bash demo/run_arm.sh baseline 1 --dry-run
```

Check what landed:

```sh
.venv/bin/python -m tamias.cli report --db demo/runs/routed-1.db   --prices prices.openrouter-sim.toml
.venv/bin/python -m tamias.cli report --db demo/runs/baseline-1.db --prices prices.openrouter-sim.toml
```

Observed on 2026-10-05, dry run:

```
routed-1.db     12 rows   3 rows where model_used != model_requested   12/12 rows with token counts
baseline-1.db   12 rows   0 rows rewritten                            5 SWITCH decisions recorded, none applied
```

---

## 1. Pick the models

```sh
.venv/bin/python demo/pick_models.py
```

Prints every catalogue model that supports `tools` and actually costs something,
grouped by provider, cheapest first within each group. Pick a **strong** and a
**cheap** model from the same provider if you can, so the switch is a like-for-like
comparison rather than a change of vendor.

Free `:free` models are deliberately **excluded** (both prices must be `> 0`).
They are real models that cost nothing, and against them every saving this demo
can compute is exactly `$0.00`. Note which provider prefix each id carries: the
pair should be picked from the table, not guessed.

## 2. Build the price sheet

```sh
.venv/bin/python demo/pick_models.py --emit-toml "PROVIDER/strong-model" "PROVIDER/cheap-model" \
  > demo/prices.toml
```

This writes real rates in USD per 1,000,000 tokens, from the catalogue, with
today's date. `cached_input` uses the published `input_cache_read` when there is
one and otherwise falls back to the input rate with a `# no cache discount known`
comment beside it. `cache_write = 0.0` is deliberate: an OpenAI-style upstream
carries no cache-write token count, so a nonzero rate would leave every request
UNKNOWN instead of priced.

Point the config at it:

```sh
cp demo/config.env.example demo/config.env
$EDITOR demo/config.env      # STRONG_MODEL=, CHEAP_MODEL=, PRICES=
```

## 3. Put the key in the terminal, not in a file

```sh
read -rsp 'OpenRouter API key: ' OPENROUTER_API_KEY; echo
export OPENROUTER_API_KEY
```

`read -rsp` keeps it off the screen and out of your shell history. tamias has no
`--api-key` flag and stores no credential: the proxy forwards OpenCode's
`Authorization` header byte-for-byte, so the key goes OpenCode → tamias →
OpenRouter and is never read from a tamias-side setting. **Anything that can
reach port 8000 can spend your key**, so leave `--host` on `127.0.0.1`.

Create the key at <https://openrouter.ai/settings/keys>, put a credit limit on it
if your plan allows one, and delete it when you are done.

## 4. Run 3 baseline and 3 routed arms

One per line; each takes a couple of minutes, and each regenerates the task from
scratch into `/tmp/tamias-demo-task`:

```sh
for n in 1 2 3; do bash demo/run_arm.sh baseline $n; done
for n in 1 2 3; do bash demo/run_arm.sh routed   $n; done
```

Each arm:

1. regenerates the buggy task project into `/tmp/tamias-demo-task`;
2. starts the proxy in the background against `UPSTREAM` with prices `PRICES`;
3. prints `key set` or `key missing` — **only** that, never the value;
4. writes an `opencode.json` in the task dir pointing an
   `@ai-sdk/openai-compatible` provider at the proxy (per
   [docs/OPENROUTER.md](../docs/OPENROUTER.md));
5. runs `timeout 900 opencode run --auto "$(cat prompt.txt)"` from the task dir;
6. runs the task's own pytest and records whether it passed;
7. kills the proxy **by the recorded PID** — never by `pkill`, because other
   agents share this machine;
8. writes `demo/runs/<arm>-<n>.json`.

The score for each arm:

```sh
cat demo/runs/routed-1.json
```

```json
{
  "arm": "routed",
  "n": 1,
  "dry_run": false,
  "tests_passed": true,
  "wall_seconds": 214,
  "models": { "strong": "...", "cheap": "..." },
  "price_sheet": "demo/prices.toml",
  "db": "/…/demo/runs/routed-1.db"
}
```

## 5. Look at the dashboard

Once `tamias.dashboard` is merged, live and replay:

```sh
# live: one arm, priced as it streams in
.venv/bin/python -m tamias.dashboard --db demo/runs/routed-1.db --prices demo/prices.toml

# replay: this arm against the baseline that shares its task
.venv/bin/python -m tamias.dashboard --db demo/runs/routed-1.db --prices demo/prices.toml \
  --compare-db demo/runs/baseline-1.db
```

`tamias.dashboard` is **not in this branch yet**, so both of these commands will
fail with `No module named tamias.dashboard` until it lands. Until then use
`tamias report`, which reads the same databases read-only:

```sh
.venv/bin/python -m tamias.cli report --db demo/runs/routed-1.db --prices demo/prices.toml
```

---

## Fallback: if the network or the free tier fails

If OpenRouter is unreachable, rate-limited, or the account is throttled, **do not
retry in a loop** — wait 30 s, retry once, and if it throttles again stop. A
suspended account is worse than a missing demo. Then:

```sh
# 1. Rehearse instead. No key, no network, real proxy, real router.
bash demo/run_arm.sh routed 1 --dry-run
bash demo/run_arm.sh baseline 1 --dry-run

# 2. Replay whatever databases you already have. `tamias report` reads a sqlite
#    file offline, so yesterday's runs are still worth something.
.venv/bin/python -m tamias.cli report --db demo/runs/routed-1.db   --prices demo/prices.toml
.venv/bin/python -m tamias.cli report --db demo/runs/baseline-1.db --prices demo/prices.toml

# 3. Inspect any run's rows without the report.
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("demo/runs/routed-1.db")
for row in c.execute(
    "SELECT id, model_requested, model_used, cost_usd, decision_action FROM requests ORDER BY id"
):
    print(row)
PY
```

The dry-run databases are worth keeping for exactly this reason: they replay the
router's decisions with no dependency on anyone's network.

---

## Honest limits

Read this before quoting any number from this demo.

- **One small task, six modules, seven functions.** This is a smoke test for the
  proxy and the router, not a benchmark. Nobody has published a quality result
  from it and neither will I.
- **Three runs per arm is not a sample.** Three numbers have no confidence
  interval. If the routed arm fixed the task 3 times out of 3, that is not
  evidence that routing is 100% as good as not routing.
- **The prices in the dry runs are invented.** `prices.openrouter-sim.toml`
  carries `simulated = true` and every amount derived from it is stamped
  `SIMULATED PRICES, NOT REAL SAVINGS`. The model *ids* are real; the *rates* are
  made up. A real sheet from `pick_models.py --emit-toml` has real rates and
  drops that stamp.
- **I quote list prices, not a quality benchmark.** `pick_models.py` prints what
  OpenRouter publishes per token. It says nothing about how well a model does
  this task, and a cheap model that needs three attempts to fix `median` is worse
  than a strong one that needs one, whatever the sheet says.
- **The two arms did not do the same amount of work, so their totals are not
  comparable.** Both arms take `--inject-usage` and both price every row, so this
  is not a measurability gap any more — but the row counts differ:

  ```
  routed-1.db     12 rows   $0.001202   (12 of 12 rows priced)
  baseline-1.db   12 rows   $0.001559   (12 of 12 rows priced)
  routed-2.db     10 rows   $0.239832   (10 of 10 rows priced)
  baseline-2.db    7 rows   $0.180645    (7 of 7 rows priced)
  baseline-3.db    4 rows   $0.083058    (4 of 4 rows priced)
  ```

  A dollar difference between the arms mostly measures how much the agent
  happened to do, not what routing saved. Compare `model_used` against
  `model_requested` and the `decision_action` counts instead; those do not depend
  on how many rows each arm wrote. Every figure above is hypothetical: all rows
  are `:free` models, so the real billed cost is `$0`, and `tamias report` puts
  `[SIMULATED PRICES, NOT REAL SAVINGS]` beside each of them — copy the label
  along with the number.
- **`--effort-policy` is wired up on this branch.** `config.env`'s
  `EFFORT_POLICY` setting is passed through by the routed arm and
  `tamias serve --help` lists both `--effort-policy {off,easy-low}` and
  `--effort-style {openrouter,openai}`, so leaving it empty is no longer
  required. It stays opt-in: with it empty the proxy forwards the body
  byte-for-byte. **What is verified:** with `--router-mode active
  --effort-policy easy-low`, a live free-model run recorded
  `effort_used = high` on the planning turn and `effort_used = low` on the
  easy-tool turn, each matching `decision_target_effort`, and all three rows
  returned 200 — so the policy is applied and persisted end to end. **What is
  not:** whether the provider *honoured* the effort field. The log records the
  requested and forwarded setting, not the provider's behaviour, so an effort
  change is not evidence of an effort effect.
- **The dashboard runs on this branch.** Section 5's
  `tamias dashboard --db … --prices …` (and the equivalent
  `python -m tamias.dashboard …`) both work. `tamias report` reads the same files
  without a browser if you would rather stay in the terminal.
- **A run can still fail for reasons that have nothing to do with routing**: a
  free tier 429, a model id retired from the catalogue, or an agent that runs out
  of its 900 s budget. Read the `status` column before drawing any conclusion
  from a row count.