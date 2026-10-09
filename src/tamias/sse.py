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
    """Assembles SSE events from byte streams.

    Standard SSE: events are separated by blank lines. Multiple data lines
    within an event are joined with newlines. Lines starting with ':' are
    comments and ignored. Field values may have a leading space stripped.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, chunk: bytes) -> Iterator[SseEvent]:
        """Feed bytes and yield completed events."""
        if chunk:
            self._buffer.extend(chunk)
        buf = bytes(self._buffer)
        # Find event boundaries (blank line: \n\n or \r\n\r\n)
        start = 0
        i = 0
        n = len(buf)
        while i < n:
            # Check for blank line
            if i + 1 < n and buf[i] == ord('\n') and buf[i + 1] == ord('\n'):
                event_bytes = buf[start : i + 2]
                self._buffer = bytearray(buf[i + 2 :])
                event = self._parse_event(event_bytes)
                if event is not None:
                    yield event
                buf = bytes(self._buffer)
                n = len(buf)
                i = 0
                start = 0
                continue
            if i + 3 < n and buf[i : i + 2] == b'\r\n' and buf[i + 2 : i + 4] == b'\r\n':
                event_bytes = buf[start : i + 4]
                self._buffer = bytearray(buf[i + 4 :])
                event = self._parse_event(event_bytes)
                if event is not None:
                    yield event
                buf = bytes(self._buffer)
                n = len(buf)
                i = 0
                start = 0
                continue
            i += 1

    def flush(self) -> Iterator[SseEvent]:
        """Flush remaining buffer."""
        if self._buffer:
            buf = bytes(self._buffer)
            self._buffer.clear()
            event = self._parse_event(buf)
            if event is not None:
                yield event

    def _parse_event(self, event_bytes: bytes) -> SseEvent | None:
        # Normalize to LF for line splitting
        text = event_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        lines = text.split(b"\n")
        data_parts: list[bytes] = []
        event_name = None
        event_id = None
        retry = None
        for line in lines:
            if not line:
                continue  # skip empty lines
            if line.startswith(b":"):
                continue  # comment
            if b":" not in line:
                # According to SSE, lines with no colon are treated as field name with empty value
                # or just ignored? Typically "data" alone is odd; but handle
                continue
            field, value = line.split(b":", 1)
            # Strip leading space if present (but not tab per spec - though we also accept)
            if value and value[0:1] == b" ":
                value = value[1:]
            field_name = field.decode("utf-8", errors="replace")
            if field_name == "data":
                data_parts.append(value)
            elif field_name == "event":
                event_name = value.decode("utf-8", errors="replace") if value else None
            elif field_name == "id":
                event_id = value.decode("utf-8", errors="replace") if value else None
            elif field_name == "retry":
                try:
                    retry_str = value.decode("utf-8", errors="replace").strip()
                    retry = int(retry_str) if retry_str else None
                except Exception:
                    retry = None
        if data_parts:
            data = b"\n".join(data_parts)
            return SseEvent(data=data, event=event_name, id=event_id, retry=retry)
        # Event with no data lines (e.g., only comments or only event/id fields)
        return None
