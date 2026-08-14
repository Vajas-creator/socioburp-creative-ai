"""
Explicit unlimited-access allowlist for internal test numbers -- see the
Aug 2026 consolidated fix list. NOT a general credit-system bypass:
listed numbers still go through the FULL normal flow (onboarding,
negotiation, quality gate, delivery, etc.) -- only credit deduction and
the quality-check regen-attempt cap are skipped for them. Every other
gate (content policy, rate limiting, message routing) still applies.

Deliberately keyed on the phone number itself (msg.sender / Business.phone),
checked directly at each call site rather than as a flag stored on the
Business row -- a listed number's Business record can be deleted and
recreated from scratch (e.g. to reset it to a fresh-user state for
testing) without ever losing or needing to re-grant unlimited access,
since the allowlist membership lives here in code, not in any row the
reset would touch.
"""

UNLIMITED_ACCESS_PHONES = {
    "919818069317",
}


def has_unlimited_access(phone: str) -> bool:
    return phone in UNLIMITED_ACCESS_PHONES
