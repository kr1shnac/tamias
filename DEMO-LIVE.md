# Live paid-OpenRouter handoff

This is a handoff, not evidence of a paid live request.  The only supplied
OpenRouter sheet lists free models, and the supplied agent interface gives no
paid model identifier or matching list-price sheet.  Do not substitute a
guessed model, sheet, or rate.  `docs/BILLED-COST.md` also marks text-chat cost
and generation-ID placement **UNVERIFIED**; absent values remain UNKNOWN.

## Terminal A — proxy

**UNTESTED against paid OpenRouter.** The command form and the OpenRouter base
URL are taken from the repository's OpenRouter/README guidance; its proxy write
path, with a fake upstream, is covered by
`tests/test_e2e.py::test_dashboard_updates_while_the_proxy_writes_rows`.
Replace only the two bracketed values after choosing a paid model and a price
sheet whose stored model key matches it exactly.

```sh
.venv/bin/python -m tamias.cli serve \
  --upstream https://openrouter.ai/api \
  --prices <paid-model-price-sheet.toml> \
  --db /tmp/or-paid.db \
  --host 127.0.0.1 \
  --port 8000 \
  --router-mode shadow \
  --request-usage-cost
```

`--router-mode shadow`, host/port, and database wiring are the same serve shape
used by `demo/run_arm.sh`; `--request-usage-cost` is intentionally explicit and
opt-in.  The agent, not this command, supplies its bearer credential.

## Terminal B — dashboard

**VERIFIED with a fake upstream** by
`tests/test_e2e.py::test_dashboard_updates_while_the_proxy_writes_rows`: a
dashboard created before proxy traffic sees the committed request row.

```sh
.venv/bin/python -m tamias.dashboard \
  --db /tmp/or-paid.db \
  --prices <paid-model-price-sheet.toml> \
  --port 8010
```

Open `http://127.0.0.1:8010`. It polls while the proxy writes. Billed money is
shown only from `provider_cost_usd`; no billed value means `billed cost: UNKNOWN`.

## Terminal C — agent

**UNTESTED.** `~/Desktop/krish-agent/agent.py`, `TAMIAS_BASE_URL`, and
`AGENT_MODEL` were supplied for this handoff; they do not appear in
`README.md` or `demo/run_arm.sh`. The base URL follows README's documented
OpenAI-compatible proxy URL. Supply an actual paid OpenRouter chat-completions
model ID; no model has been guessed here.

```sh
export TAMIAS_BASE_URL=http://localhost:8000/v1
export AGENT_MODEL='<paid OpenRouter chat-completions model id>'
.venv/bin/python ~/Desktop/krish-agent/agent.py
```

## Reset

**UNTESTED for `~/Desktop/krish-agent`.** No reset command for that workspace
exists in `README.md` or `demo/run_arm.sh`, so none is guessed here. The demo
script resets a different, generated workspace only: it removes
`/tmp/tamias-demo-task` and regenerates it with `demo/make_task.py`; do not
apply that command to the agent workspace.
