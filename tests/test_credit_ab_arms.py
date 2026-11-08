"""Tests for the live sweep's arm wiring: the defect that made D1 ship.

``scripts/credit_ab_check.py`` shipped a live sweep whose arms were labels
only: every arm sent ``args.strong_model`` and only the session header
differed, so the sweep could never rank candidates -- it measured run-to-run
noise.  The live path had **no test at all**, which is why the defect
survived.

These tests drive the real arm runner against the in-process mock upstream.
No network, no key, no spend.  ``test_the_arms_really_differ`` is the one
that fails against the old code.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path
from typing import Any

# A made-up value, not a credential: the point is that the script cannot leak
# whatever string it is given.
FAKE_KEY = "sk-live-0000000000000000-not-a-real-key"


class _LocalUpstream:
    """A real HTTP server, because the bypass arm genuinely leaves the process.

    The static arm is the one that never traverses Tamias, so it has to reach
    a provider over a real socket.  This is that provider, locally.
    """

    def __init__(self) -> None:
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        payload = json.dumps(
            {
                "id": "chatcmpl-local",
                "object": "chat.completion",
                "created": 0,
                "model": "mock-cheap",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                          "total_tokens": 120},
            }
        ).encode()

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - http.server's naming
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "credit_ab_check.py"
    spec = importlib.util.spec_from_file_location("credit_ab_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sheet(tmp_path: Path) -> Path:
    path = tmp_path / "prices.toml"
    path.write_text(
        'date = "2026-10-21"\nsimulated = true\n\n'
        '["mock-strong"]\ninput = 3.0\noutput = 15.0\ncached_input = 0.3\n\n'
        '["mock-cheap"]\ninput = 0.5\noutput = 1.5\ncached_input = 0.05\n'
    )
    return path


def _registry(tmp_path: Path) -> Path:
    path = tmp_path / "models.toml"
    path.write_text(
        '[[models]]\nid = "mock-strong"\nprovider = "openrouter"\n'
        "context_window = 128000\ntools = true\n\n"
        '[[models]]\nid = "mock-cheap"\nprovider = "openrouter"\n'
        "context_window = 128000\ntools = true\n"
    )
    return path


def _args(tmp_path: Path, **overrides: Any) -> argparse.Namespace:
    args = argparse.Namespace(
        strong_model="mock-strong",
        cheap_model="mock-cheap",
        upstream=overrides.pop("upstream", "http://mock-upstream.invalid"),
        prices=str(_sheet(tmp_path)),
        registry=str(_registry(tmp_path)),
        min_gap=0,
        task="list files",
        oracle="true",
        oracle_cwd=None,
        probes=1,
        min_detectable=0.000001,
        repeats=1,
        candidates="mock-cheap",
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


def _patched(module, monkeypatch):
    """Point the script's proxy at the in-process mock upstream."""
    from mock_upstream import StreamingASGITransport, create_mock_upstream

    import tamias.proxy as proxy_module

    original = proxy_module.create_app

    def create_app(upstream, store, sheet, mode, **kwargs):
        kwargs["transport"] = StreamingASGITransport(create_mock_upstream())
        return original(upstream, store, sheet, mode, **kwargs)

    monkeypatch.setattr(proxy_module, "create_app", create_app)
    return module


def test_the_arms_really_differ(tmp_path: Path, monkeypatch) -> None:
    """Each arm must do a genuinely different thing, not just be labelled."""
    module = _module()
    _patched(module, monkeypatch)
    module._SECRET = FAKE_KEY
    upstream = _LocalUpstream()
    try:
        args = _args(tmp_path, upstream=upstream.base_url)
        _run_arms(module, args, tmp_path)
    finally:
        upstream.close()


def _run_arms(module, args, tmp_path: Path) -> None:
    """Run the three arms and assert they differ.  Raises on the first failure."""

    off = module.run_arm(args, module.ArmSpec("off", "off", ""), tmp_path / "off.db")
    active = module.run_arm(
        args, module.ArmSpec("active:mock-cheap", "active", "mock-cheap"),
        tmp_path / "active.db",
    )
    static = module.run_arm(
        args, module.ArmSpec("static:mock-cheap", "bypass", "mock-cheap"),
        tmp_path / "static.db",
    )

    assert off.error is None, off.error
    assert active.error is None, active.error
    assert static.error is None, static.error

    # The off arm must not have rewritten anything.
    assert off.rewritten == 0
    # The active arm must have rewritten to the declared cheap target.
    assert active.rewritten >= 1
    # The static arm must not have touched the request log at all.
    assert static.rows == 0
    # And the two proxied arms must be distinguishable in their logs.
    assert off.rows >= 1 and active.rows >= 1


def test_the_off_arm_never_rewrites(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    _patched(module, monkeypatch)
    module._SECRET = FAKE_KEY

    outcome = module.run_arm(
        _args(tmp_path), module.ArmSpec("off", "off", ""), tmp_path / "off.db"
    )

    assert outcome.error is None, outcome.error
    assert outcome.rewritten == 0
    rows = module._read_rows(tmp_path / "off.db")
    assert rows
    for row in rows:
        assert row["model_used"] == row["model_requested"]
        assert row["router_mode"] == "off"


def test_the_active_arm_only_rewrites_to_the_declared_target(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    _patched(module, monkeypatch)
    module._SECRET = FAKE_KEY

    outcome = module.run_arm(
        _args(tmp_path),
        module.ArmSpec("active:mock-cheap", "active", "mock-cheap"),
        tmp_path / "active.db",
    )

    assert outcome.error is None, outcome.error
    for row in module._read_rows(tmp_path / "active.db"):
        if row["model_used"] != row["model_requested"]:
            assert row["model_used"] == "mock-cheap"


def test_each_repetition_gets_its_own_database_and_session_id(
    tmp_path: Path, monkeypatch
) -> None:
    """One shared log would give every arm the same session id."""
    module = _module()
    _patched(module, monkeypatch)
    module._SECRET = FAKE_KEY
    args = _args(tmp_path)
    arm = module.ArmSpec("active:mock-cheap", "active", "mock-cheap")

    first = module.run_arm(args, arm, tmp_path / "r0.db")
    second = module.run_arm(args, arm, tmp_path / "r1.db")

    assert first.error is None, first.error
    assert second.error is None, second.error
    sessions = {
        row["session_id"]
        for db in ("r0.db", "r1.db")
        for row in module._read_rows(tmp_path / db)
    }
    assert len(sessions) == 2, "two repetitions must not share a session id"


def test_the_bypass_arm_writes_nothing_to_the_request_log(
    tmp_path: Path, monkeypatch
) -> None:
    module = _module()
    _patched(module, monkeypatch)
    module._SECRET = FAKE_KEY
    upstream = _LocalUpstream()
    try:
        outcome = module.run_arm(
            _args(tmp_path, upstream=upstream.base_url),
            module.ArmSpec("static:mock-cheap", "bypass", "mock-cheap"),
            tmp_path / "static.db",
        )
    finally:
        upstream.close()

    assert outcome.error is None, outcome.error
    assert module._read_rows(tmp_path / "static.db") == []


def test_build_arms_pairs_every_candidate_with_a_static_arm() -> None:
    module = _module()

    arms = module.build_arms(["a", "b"])

    assert [arm.name for arm in arms] == [
        "off",
        "active:a",
        "static:a",
        "active:b",
        "static:b",
    ]
    assert all(arm.proxied for arm in arms)
    assert module.build_arms(["a"])[2].mode == "bypass"


def test_the_oracle_never_returns_the_output_text(tmp_path: Path) -> None:
    """D6: the oracle's output can hold a prompt, a response, a path or a secret."""
    module = _module()

    passed, reason = module.run_oracle(
        "sh -c \"echo 'SECRET-TOKEN-abc123'; exit 3\""
    )

    assert passed is False
    assert "SECRET-TOKEN-abc123" not in reason
    assert "exit 3" in reason
    assert "sha256" in reason


def test_the_oracle_runs_without_a_shell(tmp_path: Path) -> None:
    """A spec-supplied command must not be interpreted by a shell."""
    module = _module()

    passed, reason = module.run_oracle("echo hi; touch /tmp/should-not-exist")

    # The whole string is one argument to echo, so no file is created.
    assert passed is True, reason
    assert not Path("/tmp/should-not-exist").exists()


def test_the_oracle_reports_a_timeout_without_the_output(tmp_path: Path) -> None:
    module = _module()

    passed, reason = module.run_oracle("sleep 5", timeout=0.25)

    assert passed is False
    assert "timed out" in reason



class _RecordingTransport:
    """An httpx transport that keeps every request body it was handed.

    The arms run through the real proxy against the mock upstream, so the only
    place the request body can be observed is the transport itself.
    """

    def __init__(self, app, seen: list[dict]):
        from mock_upstream import StreamingASGITransport

        self.inner = StreamingASGITransport(app)
        self.seen = seen

    async def handle_async_request(self, request):
        import json

        body = await request.aread()
        try:
            self.seen.append(json.loads(body))
        except ValueError:
            self.seen.append({})
        return await self.inner.handle_async_request(request)

    def handle_request(self, request):
        raise AssertionError("the arms must use the async transport")


def _capture_bodies(module, monkeypatch) -> list[dict]:
    from mock_upstream import create_mock_upstream

    import tamias.proxy as proxy_module

    seen: list[dict] = []
    original = proxy_module.create_app

    def create_app(upstream, store, sheet, mode, **kwargs):
        kwargs["transport"] = _RecordingTransport(create_mock_upstream(), seen)
        return original(upstream, store, sheet, mode, **kwargs)

    monkeypatch.setattr(proxy_module, "create_app", create_app)
    return seen


def test_max_tokens_reaches_the_wire_and_is_omitted_when_unset(
    tmp_path: Path, monkeypatch
) -> None:
    """The live failure this defends: an unbounded request made the provider
    reserve the model's whole context against the balance, so the arm was
    refused 402 before the task was ever attempted."""
    module = _module()
    module._SECRET = FAKE_KEY

    seen = _capture_bodies(module, monkeypatch)
    outcome = module.run_arm(
        _args(tmp_path, max_tokens=64),
        module.ArmSpec("off", "off", ""),
        tmp_path / "bounded.db",
    )
    assert outcome.error is None, outcome.error
    assert seen, "no request reached the upstream"
    bounded = seen[:2]
    assert all(body.get("max_tokens") == 64 for body in bounded), bounded

    # A second arm reuses the same patch (and the same list, hence the slice):
    # without the flag the request is still buildable, it is just unbounded.
    outcome = module.run_arm(
        _args(tmp_path, max_tokens=None),
        module.ArmSpec("off", "off", ""),
        tmp_path / "unbounded.db",
    )
    assert outcome.error is None, outcome.error
    unbounded = seen[2:]
    assert unbounded and all("max_tokens" not in body for body in unbounded), unbounded
