from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tamias.sse import SseAssembler  # noqa: E402


def test_assembler_joins_data_lines_and_ignores_comments() -> None:
    assembler = SseAssembler()

    events = list(
        assembler.feed(
            b": keepalive\n"
            b"event: message\n"
            b"data: {\"a\":1}\n"
            b"data: {\"b\":2}\n\n"
        )
    )

    assert len(events) == 1
    assert events[0].event == "message"
    assert events[0].data == b'{"a":1}\n{"b":2}'


def test_assembler_handles_crlf_and_byte_boundaries() -> None:
    assembler = SseAssembler()
    chunks = [b"data: {\"text\":", b"\"ok\"}\r", b"\n\r", b"\n"]

    events = [event for chunk in chunks for event in assembler.feed(chunk)]

    assert [event.data for event in events] == [b'{"text":"ok"}']


def test_assembler_preserves_done_and_discards_comment_only_events() -> None:
    assembler = SseAssembler()

    events = list(assembler.feed(b": comment\n\n"))
    events.extend(assembler.feed(b"data: [DONE]\n\n"))

    assert [event.data for event in events] == [b"[DONE]"]
