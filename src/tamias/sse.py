"""Server-Sent Events (SSE) parsing utilities."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass


@dataclass
class SseEvent:
    """A single SSE event assembled from one or more data lines."""

    data: bytes | None = None
    event: str | None = None
    id: str | None = None
    retry: int | None = None


class SseAssembler:
    """Assembles SSE events from byte streams."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> Iterator[SseEvent]:
        """Feed bytes and yield completed events."""
        if chunk:
            self._buffer.extend(chunk)
        # Process events separated by blank lines (CRLF or LF)
        # For simplicity, split by \r\n\r\n and \n\n
        buf = bytes(self._buffer)
        # Try to find event boundaries
        # Handle both \r\n and \n
        i = 0
        while i < len(buf):
            # Look for blank line (event boundary)
            # Check for \r\n\r\n or \n\n
            if i + 3 <= len(buf) and buf[i : i + 4] == b"\r\n\r\n":
                event_bytes = buf[: i + 4]
                self._buffer = bytearray(buf[i + 4 :])
                event = self._parse_event(event_bytes)
                if event is not None:
                    yield event
                buf = bytes(self._buffer)
                i = 0
                continue
            if i + 1 <= len(buf) and buf[i : i + 2] == b"\n\n":
                event_bytes = buf[: i + 2]
                self._buffer = bytearray(buf[i + 2 :])
                event = self._parse_event(event_bytes)
                if event is not None:
                    yield event
                buf = bytes(self._buffer)
                i = 0
                continue
            i += 1
            if i > len(buf):
                break

    def flush(self) -> Iterator[SseEvent]:
        """Flush remaining buffer."""
        if self._buffer:
            buf = bytes(self._buffer)
            self._buffer.clear()
            event = self._parse_event(buf)
            if event is not None:
                yield event

    def _parse_event(self, event_bytes: bytes) -> SseEvent | None:
        # Parse lines in the event
        # Normalize line endings
        text = event_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = text.split(b"\n")
        data_parts: list[bytes] = []
        event_name = None
        event_id = None
        retry = None
        for line in lines:
            if not line:
                continue  # skip empty lines in the middle
            # Comment or field
            if line.startswith(b":"):
                continue  # comment
            # Split field name and value
            if b":" in line:
                field, value = line.split(b":", 1)
                if value.startswith(b" "):
                    value = value[1:]
                elif value.startswith(b"\t"):
                    value = value[1:]
                field_name = field.decode("utf-8", errors="replace")
                if field_name == "data":
                    data_parts.append(value)
                elif field_name == "event":
                    event_name = value.decode("utf-8", errors="replace")
                elif field_name == "id":
                    event_id = value.decode("utf-8", errors="replace")
                elif field_name == "retry":
                    try:
                        retry = int(value.decode("utf-8", errors="replace").strip())
                    except Exception:
                        retry = None
        if data_parts:
            data = b"\n".join(data_parts)
            return SseEvent(data=data, event=event_name, id=event_id, retry=retry)
        # Empty event (no data) - per SSE, events may be empty; but if no data, maybe skip?
        # But comments/other fields only - return event with None data?
        return SseEvent(data=None, event=event_name, id=event_id, retry=retry)
