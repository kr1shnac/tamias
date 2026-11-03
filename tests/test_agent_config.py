from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tamias.cli import agent_config


def test_agent_snippets_have_required_endpoints() -> None:
    assert agent_config("claude-code", 9, "p") == "export ANTHROPIC_BASE_URL=http://127.0.0.1:9/p/p"
    assert 'base_url = "http://127.0.0.1:9/p/p/v1"' in agent_config("codex", 9, "p")
    assert 'wire_api = "responses"' in agent_config("codex", 9, "p")
    assert '"baseURL":"http://127.0.0.1:9/p/p/v1"' in agent_config("opencode", 9, "p")


def test_opencode_provider_lists_the_models_it_is_given() -> None:
    """A provider block without models is accepted by OpenCode but shows nothing."""
    config = json.loads(agent_config("opencode", 9, None, ("a-free", "b-free")))

    provider = config["provider"]["tamias"]
    assert provider["models"] == {
        "a-free": {"name": "a-free"},
        "b-free": {"name": "b-free"},
    }
    assert provider["options"]["baseURL"] == "http://127.0.0.1:9/v1"
