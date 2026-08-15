"""
Feature flag for the new agentic conversational bot (see app/engine/agent.py)
-- Aug 2026, replacing the classifier-cascade + state-machine architecture
with a single continuous Claude conversation that reasons and calls tools
on its own, per Vajas's "it should be exactly like chatting with ChatGPT"
request.

Deliberately a SEPARATE allowlist from app/allowlist.py's unlimited-access
one, even though both currently list the same test number -- one flag is
about credit/billing exemption, this one is about which code path a
business's messages even go through. They're different concerns that
happen to currently apply to the same number; keeping them as separate,
independently-toggleable checks avoids conflating "this number doesn't
pay" with "this number gets the experimental bot."

Per the explicit rollout decision: build and validate this thoroughly
against the test number (and the qa/customer_simulator.py harness) BEFORE
any real customer ever reaches it. Every business not in this list keeps
using the existing, already-hardened router.py pipeline completely
unchanged and unaffected.
"""

AGENTIC_BETA_PHONES = {
    "919818069317",
}


def is_enabled(phone: str) -> bool:
    return phone in AGENTIC_BETA_PHONES
