"""
Forces IPv4-only DNS resolution for the entire process.

Why: production connection failures to api.anthropic.com specifically
(while other outbound calls, e.g. graph.facebook.com, succeeded fine in
the same request) match a well-documented bug class: api.anthropic.com
has an IPv6 (AAAA) DNS record, and on hosts/networks with
"half-configured" IPv6 (IPv6 locally advertised/resolvable but no real
working upstream route — common on shared/free hosting tiers), some HTTP
client stacks try IPv6 first, get no usable response, and don't fall
back to IPv4 cleanly — surfacing as exactly the
anthropic.APIConnectionError("Connection error") seen in Render logs
Aug 8, 2026, with no underlying HTTP-level detail at all. See:
https://github.com/anthropics/claude-code/issues/20240 for the identical
symptom on a different client stack, and multiple matching reports on
other constrained/free hosting tiers.

Fix: monkeypatch socket.getaddrinfo, process-wide, to only ever return
IPv4 (AF_INET) results — nothing in the process can attempt an IPv6
connection afterward, regardless of what any individual HTTP client
tries to do internally. This is a known, low-risk, widely-used
workaround for exactly this bug class.

MUST be imported first in app/main.py, before any other module that
might construct an HTTP client or open a network connection — DNS
resolution happens at request time (not client-construction time), so
this only needs to run before the first real network call, but placing
it first removes any doubt.
"""
import logging
import socket

logger = logging.getLogger("socioburp.network_fix")

_original_getaddrinfo = socket.getaddrinfo


def _getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


def apply():
    """Idempotent — safe to call more than once (e.g. across test imports)."""
    if socket.getaddrinfo is _getaddrinfo_ipv4_only:
        return
    socket.getaddrinfo = _getaddrinfo_ipv4_only
    logger.info("Forced IPv4-only DNS resolution for this process (see app/network_fix.py)")


apply()
