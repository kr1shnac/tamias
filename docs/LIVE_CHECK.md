# Live check: proving the proxy works against a real upstream

`scripts/live_check.py` sends three requests through a running tamias proxy,
then reads the sqlite log back and prints what tamias recorded. It is the check
to run before trusting anything else the log tells you, including the numbers
in [OPENCODE.md](OPENCODE.md).

The three requests carry the same one-line prompt — `"Reply with the word ok"` —
and differ only in how usage was requested:

| # | request | what the upstream is asked for |
| --- | --- | --- |
| 1 | buffered | usage in the JSON body |
| 2 | streaming, **no** `stream_options` | no usage: the upstream sends no usage chunk |
| 3 | streaming, `stream_options.include_usage = true` | a trailing usage chunk |

Request 2 is the one that matters. tamias forwards the body byte-for-byte, so a
stream that never asked for usage cannot come back with any. **Its row must have
NULL token counts.** A number there would mean the proxy had injected
`stream_options` the agent never asked for — a contract violation, and one that
would change what the upstream bills for. Requests 1 and 3 must both have token
counts, because both asked for usage.

## 1. Offline first: `--mock`

No key, no socket, no money:

```sh
python scripts/live_check.py --mock
```

`--mock` starts the same mock upstream the test suite uses
(`tests/mock_upstream.py`) and wires the **real** proxy app to it in-process, so
the three probes travel the actual code path — request building, header
forwarding, SSE relay, logging — with no socket and no upstream. The database is
a temporary file that is deleted afterwards. Actual output on this machine:

```
tamias live check
  proxy   http://localhost:8000  (in-process mock upstream, no socket)
  model   big-pickle
  db      /tmp/tamias-live-check-bkrylov4/requests.db  (temporary)
  prompt  'Reply with the word ok'  x3  session live-check
  key     in-process, none needed

requests
  1 buffered                     HTTP 200  buffered JSON  usage=yes
  2 stream, no stream_options    HTTP 200  SSE  frames=4  usage=no  done=yes
  3 stream, include_usage=true   HTTP 200  SSE  frames=5  usage=yes  done=yes

logged rows in /tmp/tamias-live-check-bkrylov4/requests.db (newest 3, oldest first)
  #  model_requested  input  output  cached  cost   decision  session_id
  -  ---------------  -----  ------  ------  -----  --------  ----------
  1  big-pickle       11     7       3       $0.00  STAY      live-check
  2  big-pickle       ?      ?       ?       $0.00  STAY      live-check
  3  big-pickle       11     7       3       $0.00  STAY      live-check
  ? means UNKNOWN, which is never stored as 0

checks
  PASS  all 3 requests came back HTTP 200
  PASS  3 rows logged, one per request
  PASS  request 1 (buffered) logged token counts: input=11 output=7
  PASS  request 3 (stream, include_usage=true) logged token counts: input=11 output=7
  PASS  request 2 (stream, no stream_options) logged NULL token counts, as expected: usage was never requested
  PASS  no logged row contains the prompt text 'Reply with the word ok'
  PASS  no page of /tmp/tamias-live-check-bkrylov4/requests.db contains the prompt text either

RESULT: PASS
```

Read it as follows. The `frames` column is how many SSE `data:` frames came back
(4 without usage, 5 with — the extra one is the usage chunk). `?` is UNKNOWN,
never 0. The mock reports fixed counts (11 in, 7 out, 3 cached) and the mock
price sheet declares the model free, so `cost` is `$0.00` here; against Zen the
same column is arithmetic over Zen's real rates, or `?`.

The temporary path changes every run. Nothing else does.

## 2. Against Zen

Get a key from <https://opencode.ai/auth> and export it:

```sh
export ZEN_API_KEY=sk-...
```

tamias forwards `Authorization` untouched, so the key reaches Zen unchanged, and
the script only ever reads it from the environment.

Write a price sheet. Copy `prices.example.toml` to `prices.toml` and add the
model you are about to ask for — `big-pickle` is free on Zen, so its rates are
all `0`:

```toml
date = "2026-10-04"

[big-pickle]
input = 0.0
output = 0.0
```

Start the proxy, in a second terminal:

```sh
tamias serve \
  --upstream https://opencode.ai/zen \
  --prices prices.toml \
  --db live-check.db \
  --port 8000 \
  --router-mode shadow \
  --cheap-model deepseek-v4-flash \
  --strong-model big-pickle
```

`--upstream` must **not** include `/v1`: tamias appends
`/v1/chat/completions` itself, so this calls
`https://opencode.ai/zen/v1/chat/completions`. `--db live-check.db` matters: the
script reads that exact file, so point both at the same path. Use a throwaway
database for the check — it is metadata only, but it is still your traffic.

Then run the check:

```sh
python scripts/live_check.py \
  --proxy http://localhost:8000 \
  --model big-pickle \
  --db live-check.db
```

`--proxy`, `--model` and `--db` default to exactly those values, so
`python scripts/live_check.py` works on its own. Exit code is `0` on `PASS` and
`1` on `FAIL`, so it drops straight into CI or a Makefile.

## 3. What the checks mean

| check | what a failure means |
| --- | --- |
| all 3 requests returned HTTP 200 | the proxy, or Zen, rejected the request. The rest of the log is then not judged, because the rows on screen cannot be this run's rows. |
| 3 rows logged, one per request | a request that completed without being logged, or a log written to a different file than `--db` points at. |
| request 1 and request 3 logged token counts | Zen answered without a usage object. The request worked; the token counts are unavailable, and cost built on them is `?`. |
| request 2 logged NULL token counts | the proxy added `stream_options` the agent did not send. Do not trust any of the streaming numbers until this is explained. |
| no row contains the prompt text | the log is not metadata-only. This is the check that matters most for a proxy that sees your whole conversation. |
| no page of the database contains the prompt text | stronger than the row check: it scans every byte of the file, so text cannot hide in a deleted row or an unused page. |
| no row or page contains the `ZEN_API_KEY` value | a header leaked into the log. Delete the database. |

If Zen is reachable but a model is not served on `/v1/chat/completions`, expect
`404` on the first line — see the endpoint table in [OPENCODE.md](OPENCODE.md).
`big-pickle` is on that endpoint.

## Notes

- **The API key is never printed.** Every line the script emits passes through a
  redactor that replaces the key with `<redacted>`, so an upstream error message
  that somehow echoed it would still be safe to paste into an issue. There is a
  check for the key in the log itself, because a proxy that logs a header it
  forwards is a real failure mode.
- **The script sends real requests and they cost money.** Three one-token
  requests against a free model is effectively nothing; against a paid model,
  multiply by the rates in your price sheet. It never sends more than three.
- **The prompt is deliberately identifiable.** `"Reply with the word ok"` is a
  string that should never appear in a log, which is exactly what makes it a
  useful marker. It is not a secret and contains nothing of yours.
- **I have not run this against Zen.** No Zen API key was available on this
  machine, so the live section above is unverified end to end: the commands come
  from the Zen docs cited in [OPENCODE.md](OPENCODE.md), and the only output I
  have actually observed is the `--mock` run pasted in section 1. What *is*
  verified is the live code path itself — the same script was run against a real
  `tamias serve` listening on a socket, and it reported `RESULT: PASS` with
  identical checks.
