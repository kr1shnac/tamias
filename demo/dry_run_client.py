"""Drive a running tamias proxy with a realistic 12-request agent session.

This is what ``demo/run_arm.sh --dry-run`` calls instead of ``opencode run``.
It exists so both arms can be rehearsed with no key, no network and no agent:
the request shapes are what the router actually branches on, so a shadow run and
an active run of this client produce comparable rows in the same database
schema.

What it sends, and why each shape is here:

* a **user turn** first and again later -- rule 1, the agent is planning;
* **tool results** from ``read``, ``bash`` and ``grep``, each preceded by an
  assistant message carrying the matching ``tool_calls`` entry, so the router
  resolves the tool name the way a real client would;
* one tool result containing a **Traceback** -- rule 2, a failure is exactly
  when you want the strong model;
* a **hard tool** (``edit_file``) -- rule 4, no rule matches, stay put.

Against ``min_gap=3`` that yields both actions in one session: SWITCH requests
are recorded on every turn whose ``request_index`` has grown past the gap, and
the active arm actually rewrites some of them, so the routed database ends up
with rows whose ``model_used`` differs from ``model_requested`` while the
baseline database has none.

Every request is sent with ``stream: true`` and read to ``[DONE]``.  That is
what OpenCode does, and it is also what decides whether a row carries token
counts: read to the end and the trailing usage chunk is seen, stop early and the
row is logged with UNKNOWN counts.  See docs/OPENROUTER.md.
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_PROXY = "http://127.0.0.1:8000"
DEFAULT_SESSION = "demo-dry-run"
CHAT_PATH = "/v1/chat/completions"

STRONG_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
CHEAP_MODEL = "nvidia/nemotron-3.5-lightning:free"

SYSTEM = "You are a coding agent working in a small Python project."
USER = "Run the tests and fix every failing one. Do not edit anything under tests/."

TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "slug.py", line 3, in slugify\n'
    "    return s.replace(' ', '_')\n"
    "AssertionError: assert '__hello___world_' == 'hello-world'"
)


class Turn:
    """One request: a label, and the messages the client would have by now."""

    def __init__(self, label: str, kind: str, tool: str = "", content: str = "") -> None:
        self.label = label
        self.kind = kind
        self.tool = tool
        self.content = content

    def messages(self, history: list[dict[str, Any]], step: int) -> list[dict[str, Any]]:
        """The full conversation as it stands for this request."""
        messages: list[dict[str, Any]] = list(history)
        if self.kind == "user":
            # A fresh user turn follows a short assistant acknowledgement, which
            # is how a real client threads a second instruction into a session.
            messages.append({"role": "assistant", "content": f"on it ({step})"})
            messages.append({"role": "user", "content": USER})
        else:
            call_id = f"call_{step}"
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": self.tool, "arguments": "{}"},
                        }
                    ],
                }
            )
            messages.append({"role": "tool", "tool_call_id": call_id, "content": self.content})
        return messages


# The session, in order.  Step numbers below are 1-based request indices.
PLAN: tuple[Turn, ...] = (
    Turn("user turn", "user"),
    Turn("read", "tool", "read", "def slugify(s): ..."),
    Turn("read", "tool", "read", "def total(items): ..."),
    Turn("read", "tool", "read", "def fizzbuzz(n): ..."),
    Turn("bash", "tool", "bash", "7 failed in 0.03s"),
    Turn("read", "tool", "read", "def word_count(s): ..."),
    Turn("user turn", "user"),
    Turn("grep, failing", "tool", "grep", TRACEBACK),
    Turn("bash", "tool", "bash", "FAILED tests/test_slug.py::test_slugify"),
    Turn("read", "tool", "read", "def median(xs): ..."),
    Turn("bash", "tool", "bash", "7 failed in 0.03s"),
    Turn("edit_file (hard tool)", "tool", "edit_file", "wrote slug.py"),
)
REQUESTS = len(PLAN)


def build_body(turn: Turn, history: list[dict[str, Any]], step: int, model: str) -> dict[str, Any]:
    """One request body: a system prompt, the conversation so far, and the turn."""
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            *turn.messages(history, step),
        ],
        "stream": True,
        "tools": [{"type": "function", "function": {"name": turn.tool or "noop"}}],
    }


def send(url: str, body: dict[str, Any], session: str, timeout: float = 60.0) -> tuple[int, int]:
    """POST one request and read the stream to the end.

    Returns ``(status, frames)``.  Reading to ``[DONE]`` is deliberate: a client
    that stops early leaves the proxy nothing to log token counts from.
    """
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "accept": "text/event-stream",
            "x-tamias-session": session,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        status = response.status
        payload = response.read().decode("utf-8", "replace")
    frames = sum(1 for line in payload.splitlines() if line.startswith("data:"))
    return status, frames


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--proxy", default=DEFAULT_PROXY, help="proxy base URL")
    parser.add_argument("--model", default=STRONG_MODEL, help="model id the client asks for")
    parser.add_argument("--session", default=DEFAULT_SESSION, help="x-tamias-session value")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    url = args.proxy.rstrip("/") + CHAT_PATH
    history: list[dict[str, Any]] = []
    failures = 0

    print(f"dry-run client -> {url}")
    print(f"  model {args.model}  session {args.session}  requests {REQUESTS}")
    print()

    for index, turn in enumerate(PLAN, start=1):
        body = build_body(turn, history, index, args.model)
        try:
            status, frames = send(url, body, args.session, args.timeout)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  {index:>2} {turn.label:<22} FAILED: {exc}")
            failures += 1
            continue

        # The conversation so far is what the next request must carry.
        history = body["messages"][1:]
        print(f"  {index:>2} {turn.label:<22} HTTP {status}  frames={frames}")

    print()
    if failures:
        print(f"{failures} of {REQUESTS} requests did not complete")
        return 1
    print(f"all {REQUESTS} requests completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
