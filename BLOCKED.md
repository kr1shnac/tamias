# Validation blocker — 2026-10-07 (Codex) — RESOLVED (OpenCode)

Record of a managed-sandbox blocker and how each item was cleared. The
blocked items below are Codex's original findings; the **RESOLVED** notes
are later verification from a socket- and DNS-capable environment
(OpenCode, same repo).

## Gate before T-09

Codex could not run the suite: its sandbox forbids creating a local socket,
so `_unused_port()` in the CLI tests raised
`PermissionError: [Errno 1] Operation not permitted` at
`/usr/lib/python3.14/socket.py:236`.

**RESOLVED 2026-10-07.** The full gate runs here. It was red for a real
reason the socket limitation had hidden from Codex: the `feat/effort` merge
added `effort_requested`, `effort_used` and `decision_target_effort` as raw
`TEXT` while leaving the schema pin unchanged, and `effort_requested` was
read straight from the client request body with only an `isinstance(str)`
check — i.e. a prompt or a credential could be written into the log.
Fixed in `b51cb5a` (`store.sanitize_effort`, vocabulary admit, NULL
otherwise, 12 new cases) and the technique-specific failure reproduced by
`tests/test_response_headers.py`. Gate: **616 passed, ruff clean.**

## Tier-B rehearsal — T-17

Two proxy starts failed in Codex's sandbox with
`ERROR: could not bind on any address out of [('127.0.0.1', 8000)]`, so the
act was dropped.

**RESOLVED 2026-10-07.** Run live from this environment, which can bind
loopback. Two consecutive passes with `krish-agent` through a real
`tamias serve` on `127.0.0.1:8000` (shadow + `--inject-usage`,
`--strong-model poolside/laguna-s-2.1:free`,
`--cheap-model nvidia/nemotron-3.5-lightning:free`): 6 requests, 6×200,
one session, exit 0, `hello.py` written in both runs. The rehearsal exposed
one real bug that no in-process test caught — the proxy advertised the
upstream's `content-encoding: gzip` for a body httpx had already decoded,
so every non-streaming client call failed with `APIConnectionError`
("Connection error."). Fixed in `605dc52` plus a regression test. The
effort path was then exercised live (`--effort-policy easy-low`): the log
records `effort_used=high/low` matching `decision_target_effort`.

## Package and push — 0.1.0a3

Codex could not resolve `pypi.org`/`github.com`. Earlier work in this repo
had already pushed `master` and the `v0.1.0a2` tag to GitHub; the
`v0.1.0a3` effort-routing release remains to be pushed and uploaded.
TestPyPI still requires the user's API token (`~/.pypirc` / T-23).