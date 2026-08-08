"""
Test for app/network_fix.py — proves the patched socket.getaddrinfo()
actually filters IPv6 results, rather than just checking that SOME
function got installed.

Doesn't depend on real external DNS/network (unreliable in a sandboxed
test environment) — mocks the underlying resolver to return a realistic
dual-stack response (both AF_INET and AF_INET6 entries, matching what a
real query to a host with both A and AAAA records returns) and verifies
the patched wrapper strips the IPv6 entries regardless of what family
was requested.
"""
import socket

import app.network_fix as network_fix


def fake_dual_stack_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """
    Simulates a real resolver response for a dual-stack host (has both A
    and AAAA records) — including the real OS-level contract that
    explicitly requesting family=AF_INET filters OUT IPv6 results. A mock
    that ignores this (returns everything regardless of family) doesn't
    actually test whether the patch passes AF_INET through correctly —
    it would pass even if the patch forgot to force the family at all.
    """
    all_results = [
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", port, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.0.1", port)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::2", port, 0, 0)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("104.18.0.2", port)),
    ]
    if family in (0, socket.AF_UNSPEC):
        return all_results
    return [r for r in all_results if r[0] == family]


def run():
    print("=" * 60)
    print("TEST 0: sanity check — the mock itself behaves like real getaddrinfo")
    print("(unpatched, AF_UNSPEC returns both families; AF_INET alone filters to just IPv4)")
    print("=" * 60)
    unspec_results = fake_dual_stack_getaddrinfo("test", 443, socket.AF_UNSPEC)
    unspec_families = {r[0] for r in unspec_results}
    assert unspec_families == {socket.AF_INET, socket.AF_INET6}, f"FAIL: mock sanity check failed, got {unspec_families}"
    inet_only_results = fake_dual_stack_getaddrinfo("test", 443, socket.AF_INET)
    inet_only_families = {r[0] for r in inet_only_results}
    assert inet_only_families == {socket.AF_INET}, f"FAIL: mock should filter to IPv4 only when asked, got {inet_only_families}"
    print("PASS: mock resolver behaves like a real dual-stack resolver — safe to test the patch against it\n")

    print("=" * 60)
    print("TEST 1: patched resolver strips IPv6 entries even when family=AF_UNSPEC (both requested)")
    print("=" * 60)

    network_fix._original_getaddrinfo = fake_dual_stack_getaddrinfo
    socket.getaddrinfo = network_fix._getaddrinfo_ipv4_only

    results = socket.getaddrinfo("api.anthropic.com", 443, socket.AF_UNSPEC)
    families = {r[0] for r in results}

    assert socket.AF_INET6 not in families, f"FAIL: IPv6 entries leaked through, got families {families}"
    assert socket.AF_INET in families, f"FAIL: expected IPv4 entries to still be present, got families {families}"
    assert len(results) == 2, f"FAIL: expected exactly the 2 IPv4 entries, got {len(results)}: {results}"
    print(f"PASS: {len(results)} results, all IPv4, IPv6 entries correctly filtered out\n")

    print("=" * 60)
    print("TEST 2: idempotent — calling apply() twice doesn't double-wrap")
    print("=" * 60)
    before = socket.getaddrinfo
    network_fix.apply()
    network_fix.apply()
    after = socket.getaddrinfo
    assert before is after, "FAIL: apply() called twice should not change the function reference again"
    print("PASS: apply() is safely idempotent\n")

    print("ALL TESTS PASSED")


run()
