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
from urllib.parse import urlsplit


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


#: Hostnames a request may claim when the server is reached over loopback.
#: A page on any other site that resolves a name to 127.0.0.1 (DNS rebinding)
#: sends *its* name in ``Host``, which is not in this set and is refused.
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def host_is_allowed(host_header: str | None, extra_hosts: object = ()) -> bool:
    """True when a request's ``Host`` may be served.

    ``host_header`` is the raw ``Host`` header (``name`` or ``name:port``); the
    port is ignored.  Loopback names are always allowed; ``extra_hosts`` lets an
    operator allow the public names the proxy is deliberately bound behind.
    A missing ``Host`` (HTTP/1.0, some in-process clients) is allowed: the
    rebound-page attack *needs* a foreign name, so refusing the absent case
    would only break honest clients.
    """
    if not host_header:
        return True
    name = host_header.strip()
    if name.startswith("["):  # bracketed IPv6 literal
        name = name.partition("]")[0] + "]"
    elif ":" in name:
        name = name.rsplit(":", 1)[0]
    name = name.lower()
    if name in LOOPBACK_HOSTS:
        return True
    if is_loopback(name):
        return True
    if extra_hosts:
        allowed = {str(h).lower() for h in extra_hosts}  # type: ignore[union-attr]
        if name in allowed:
            return True
    return False


def origin_is_allowed(origin: str | None, extra_hosts: object = ()) -> bool:
    """True when a browser ``Origin`` header may reach the server.

    Browsers attach ``Origin`` to cross-site requests; a rebound page always
    does.  A same-origin request from the dashboard page (`Origin` is a loopback
    name) passes, as does a request with no ``Origin`` at all (non-browser
    clients).  Any other origin is a cross-site attempt and is refused.
    """
    if not origin:
        return True
    if origin == "null":  # file:// and sandboxed iframes
        return False
    try:
        hostname = urlsplit(origin).hostname
    except ValueError:
        return False
    if hostname is None:
        return False
    return host_is_allowed(hostname, extra_hosts)