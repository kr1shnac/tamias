"""Loopback-only binding policy shared by the proxy and the dashboard.

Both servers hold private data (bearer tokens in memory, per-session metering
on disk) and have no authentication, so the default is to bind loopback and to
**refuse** any wider bind unless the operator passes an explicit
acknowledgement flag.  Refusing beats warning: a warning scrolls past under an
``--inject-usage`` banner, but a refused bind cannot be missed.
"""

from __future__ import annotations

import ipaddress
import sys


def is_loopback(host: str) -> bool:
    """True when ``host`` resolves to loopback (127.0.0.1, ::1, localhost)."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def refuse_non_loopback(service: str, host: str, allow_non_loopback: bool) -> int:
    """Exit code for binding ``host``: 1 when refused, 0 when it may proceed.

    ``service`` names the program for the message ("serve" / "dashboard").
    A loopback host always proceeds; a wider host proceeds only when
    ``allow_non_loopback`` is true.
    """
    if is_loopback(host) or allow_non_loopback:
        return 0
    print(
        f"tamias {service}: refusing to bind {host!r}: this service has no "
        f"authentication and holds private data. Pass --allow-non-loopback "
        f"to bind it anyway (trusted network only).",
        file=sys.stderr,
    )
    return 1