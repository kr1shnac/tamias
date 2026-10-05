# Hardening findings

Bugs found by the `tests/test_hardening_*.py` suite on `feat/hardening`. Each one
is a real defect in `src/`, reproduced by a test marked
`xfail(strict=True, reason="BUG-n: ...")`, so the suite goes red the moment the
fix lands. Nothing in `src/` was changed to make these pass.

Reproduce everything with:

```
.venv/bin/pytest tests/test_hardening_router.py tests/test_hardening_pricing.py \
                 tests/test_hardening_proxy.py tests/test_hardening_privacy.py -q
```

| ID | Severity | Area | Summary |
| --- | --- | --- | --- |
| BUG-1 | high | proxy | A refused upstream connection answers 5xx but writes no request-log row |
| BUG-2 | medium | proxy | A 429 loses its `Retry-After` header, so a rate-limited client cannot back off correctly |
| BUG-3 | high | pricing | A model with an unquoted rate is billed as if that rate were zero, reporting $0.00 as fact |

## BUG-1: a refused upstream connection is never logged

- **Test:** `tests/test_hardening_proxy.py::test_connection_refused_is_logged_and_service_resumes`
- **Source:** `src/tamias/proxy.py:426` (non-streaming) and `src/tamias/proxy.py:450` (streaming)
- **Severity:** high

### Reproduction

The upstream refuses the first connection and then recovers:

```python
async def refuse_first_strong_call(request: httpx.Request) -> httpx.Response:
    if json.loads(request.content).get("model") == STRONG:
        strong_calls["n"] += 1
        if strong_calls["n"] == 1:
            raise httpx.ConnectError("[Errno 111] Connection refused", request=request)
    return await healthy.handle_async_request(request)
```

Two requests are sent on the same session. The first is refused; the second
succeeds. Observed:

```
refused.status_code == 500      # ok: there is no upstream status to pass through
served.status_code == 200       # ok: the proxy recovers, the pool is not poisoned
len(store.rows()) == 1          # FAIL: expected 2
status_of(store) == ["500", "200"]  # FAIL
```

Only the successful request leaves a row. The refused one is missing entirely.

### Why it is a bug

`chat_completions` calls `client.send(outgoing)` with no error handling, so
`httpx.ConnectError` propagates out of the route handler. Starlette's
`ServerErrorMiddleware` turns it into a 500 and `record(...)` is never reached.
The client's 5xx is therefore visible to nobody but the client, while the cost of
the tokens already spent on the request that did reach the model is unattributed.

That matters for this project specifically: the request log is the whole record of
what was spent and why. A downstream timeout, a DNS failure or a backend restart
silently punches a hole in the audit trail, and the hole is exactly where spend
happened. The startup banner promises "every chat completion produces one row in
the request log"; a failed one produces none, with no marker saying so.

### Suggested fix

Catch `httpx.HTTPError` around the send, log a row with a synthetic status such as
`502` and empty usage, and return the 5xx to the client from that one path. The
streaming path needs the same guard, since `_stream_chat` never runs if
`client.send(..., stream=True)` raises.

## BUG-2: `Retry-After` is dropped from relayed responses

- **Test:** `tests/test_hardening_proxy.py::test_upstream_429_keeps_its_retry_after_header`
- **Source:** `src/tamias/proxy.py:444-448` (non-streaming), `src/tamias/proxy.py:451-455` (streaming), `src/tamias/proxy.py:513-517` (passthrough)
- **Severity:** medium

### Reproduction

An upstream answers one request with `429` and `retry-after: 7`:

```
response.status_code == 429                          # ok
response.headers.get("retry-after") == "7"           # FAIL
# AssertionError: {'content-length': '63', 'content-type': 'application/json'}
# assert None == '7'
```

The status and the body arrive intact; every other response header is discarded.

### Why it is a bug

All three relay paths rebuild the response from its body and status and copy only
`content-type`:

```python
return Response(
    content=upstream.content,
    status_code=upstream.status_code,
    media_type=upstream.headers.get("content-type", "application/json"),
)
```

`Retry-After` is the one upstream header a client genuinely cannot do without.
Without it a well-behaved SDK falls back to its own default backoff, ignoring what
the upstream actually asked for: either it retries immediately and helps deepen the
rate limit, or it waits a fixed interval that may be far too short. The same
rebuild drops `x-request-id`, which is how a user correlates an entry in the
provider's logs with an entry in this one -- exactly what is needed to investigate
a request this proxy has just billed.

### Suggested fix

Copy the upstream headers through, minus hop-by-hop headers, in all three paths.
Note that the streaming and passthrough relays already stream the bytes unchanged,
so only the header set needs to change there.

## BUG-3: an unquoted rate is billed as zero

- **Test:** `tests/test_hardening_pricing.py::test_unquoted_rate_is_unknown_not_free`
- **Source:** `src/tamias/pricing.py:110` (the free-model shortcut), reaching the arithmetic at `src/tamias/pricing.py:238-244`
- **Severity:** high

### Reproduction

Three price sheets that do not quote every rate, each with a request whose usage
needs the unquoted rate:

| Price sheet | Usage | `usd` reported | Should be |
| --- | --- | --- | --- |
| `[gpt-4o]` (no rates at all) | 10,000 in / 2,000 out / 4,000 cached | `0.0` | UNKNOWN |
| `[gpt-4o] input = 0.0` | 10,000 in / 2,000 out | `0.0` | UNKNOWN |
| `[gpt-4o] input = 3.0` | 10,000 in / 2,000 out | `0.03` | UNKNOWN |

The formulas say it plainly:

```
usd = 0.0   'free model: all prices are 0'
usd = 0.03  'usd = (uncached_input 10000 * 3.0 + cached_input 0 * ? + 5m_writes 0 * ? +
                  1h_writes 0 * ? + output 2000 * ?) / 1000000'
```

The `?` are the rates the sheet never quoted. The third row is the clearest
evidence: 2,000 output tokens are multiplied by an unknown rate and priced at
nothing, and the report presents `$0.03` as a real measurement.

### Why it is a bug

The guard reads

```python
if all(p == 0.0 for p in all_prices if p is not None):
```

`if p is not None` drops every unquoted rate from the check, so the test becomes
vacuously true in two ways. With no rates quoted at all, nothing is left to check
and `all([])` is `True`. With one quoted rate of `0.0` and the rest unquoted, the
only rate examined is the zero one. Either way a model with an unknown rate is
declared free.

That contradicts the two rules the rest of the code is built on:
`ModelPrice` documents "A `None` rate means UNKNOWN, not free", and the report's
rule is that a missing input to the arithmetic is never guessed at zero -- the same
principle that returns UNKNOWN for a missing token count. A missing *count* yields
UNKNOWN; a missing *rate* yields $0.00.

The consequence is the worst kind for a cost tool: not a crash and not a visibly
absent number, but a confident, plausible, wrong one. A sheet with a stale or
half-finished entry under-reports spend as $0.00, and nothing in the report
distinguishes "this call was free" from "this call was never priced".

### Suggested fix

Treat a `None` rate as UNKNOWN wherever the arithmetic needs it, and restrict the
free-model shortcut to sheets where every rate is quoted and every one is zero.
The 1000-case differential test in `test_hardening_pricing.py` already generates
random sheets and will keep the two in step; its generator currently always quotes
every rate precisely so that BUG-3 is reported by this test alone, rather than by
a differential test with a disagreement baked into it.

## Checked and found clean

The rest of the suite is a negative result worth recording, since "no bugs found"
is only meaningful if the tests could have found some:

- **Router** (`tests/test_hardening_router.py`): 32 tests. Purity, determinism and
  self-consistency of every decision over 300 fixed-seed random conversations and
  five configurations, including the invariants that a user turn never switches,
  that a cheap model never flapped off, and that a decision reason never quotes the
  conversation. 20 of the 300 random conversations reach the SWITCH path, and the
  mutation check confirms the invariants bite. The 2,000-message worst-case history
  decides in about 0.1ms against the 1s budget.
- **Pricing** (excluding BUG-3): the 1000-case differential test resolves to 596
  numeric, 145 exactly-zero and 259 UNKNOWN cases, all agreeing with an oracle
  written from the contract. A mutant that inflates every known cost by 10% is
  caught in all 596 numeric cases, so the agreement is real rather than vacuous.
- **Privacy** (`tests/test_hardening_privacy.py`): 9 tests. The log schema has no
  column that could hold prose; prompt, system-prompt, reply and bearer token are
  absent from the raw bytes of the SQLite file, not merely from the known columns;
  session ids are 12 hex characters, stable within a conversation, distinct across
  conversations and distinct across credentials; `--verbose` logs decisions without
  quoting the conversation; `serve` binds `127.0.0.1` by default. A mutation check
  (storing the prompt in the session header) is detected by both searches.
- **Proxy** (`tests/test_hardening_proxy.py`): 19 tests, 17 passing and 2 xfailed
  for BUG-1 and BUG-2. 50 concurrent requests over 5 sessions produce 50 rows
  with no interleaved session state; a client disconnect mid-stream is logged
  without taking the server down; invalid JSON, a JSON array body and a body with
  no `messages` are handled; a slow upstream is waited for, logged once and does
  not delay the next request; `//evil.example/x`, `/@evil.example/` and `../..`
  path traversal all still reach the configured upstream host with the query string
  and method intact, for GET, POST and DELETE.