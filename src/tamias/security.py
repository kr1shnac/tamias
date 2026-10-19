"""Loopback-only binding policy shared by the proxy and the dashboard.

Both servers hold private data (bearer tokens in memory, per-session metering
on disk).  Authentication is **opt-in**: the proxy can verify a shared token or
company keys, the dashboard cannot.  A wider bind therefore always needs an
explicit acknowledgement flag, and the proxy additionally needs *some*
authentication configured.  Refusing beats warning: a warning scrolls past
under an ``--inject-usage`` banner, but a refused bind cannot be missed.
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


def refuse_non_loopback(
    service: str, host: str, allow_non_loopback: bool, *, authenticated: bool = False
) -> int:
    """Exit code for binding ``host``: 1 when refused, 0 when it may proceed.

    ``service`` names the program for the message ("serve" / "dashboard").
    A loopback host always proceeds; a wider host proceeds only when
    ``allow_non_loopback`` is true.  The refusal message names only what is
    actually missing: when ``authenticated`` is true the service does have
    authentication, so the message must not claim otherwise.
    """
    if is_loopback(host) or allow_non_loopback:
        return 0
    if authenticated:
        reason = (
            "binding a non-loopback host needs an explicit acknowledgement "
            "even with authentication configured"
        )
    else:
        reason = "this service has no authentication and holds private data"
    print(
        f"tamias {service}: refusing to bind {host!r}: {reason}. "
        f"Pass --allow-non-loopback to bind it anyway (trusted network only).",
        file=sys.stderr,
    )
    return 1