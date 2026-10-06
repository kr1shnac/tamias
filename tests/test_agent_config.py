from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tamias.cli import agent_config


def test_agent_snippets_have_required_endpoints() -> None:
    assert agent_config("claude-code", 9, "p") == "export ANTHROPIC_BASE_URL=http://127.0.0.1:9/p/p"
    assert 'base_url = "http://127.0.0.1:9/p/p/v1"' in agent_config("codex", 9, "p")
    assert 'wire_api = "responses"' in agent_config("codex", 9, "p")
    assert '"baseURL":"http://127.0.0.1:9/p/p/v1"' in agent_config("opencode", 9, "p")
