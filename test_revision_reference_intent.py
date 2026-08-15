"""
Test for the Aug 2026 "chat history isn't recorded / reusing a prior
prompt doesn't work / it's not smart enough" feedback.

Root cause traced: app/engine/image_history.py's resolve_reference() (the
short-term image-memory feature from earlier this session) is only ever
CONSULTED from orchestrator.generate()'s REVISE branch (see
orchestrator.py line ~845) -- and whether a message gets classified as
REVISE at all is entirely decided by app/engine/intent.py's classifier.
Its SYSTEM_PROMPT's REVISE examples were narrowly "adjust the very last
thing" phrasings ("make it more premium", "change the color") with
nothing covering "reuse/reference something from earlier" phrasings like
"take the prompt from our last chat", "use the one from before", "same as
last time", or "the second one". Anything phrased that way fell through
to QUESTION/OTHER and got the generic menu reply -- is_revision was never
even True, so image_history was never consulted at all, regardless of how
well recording or resolution themselves worked. Confirmed images/prompts
WERE always being recorded correctly (image_history.record_image() is
called from every generation path); the gap was purely on the
classification gate deciding whether to ever look at that history.

This locks in the broadened REVISE definition/examples so a future edit
can't narrow it back down. There's no code branch to unit-test the live
model's actual classification decision against (same limitation as the
other prompt-content tests this session) -- this checks the instruction
text itself, matching the pattern test_carousel_no_collage.py and
test_creative_quality_fixes.py already use for prompt_builder.py/
quality.py/caption.py.
"""
import sys
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_revision_reference_intent.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

from app.engine import intent  # noqa: E402


def test_revise_covers_reference_and_reuse_phrasing():
    print("=" * 60)
    print("TEST 1: intent.py's REVISE definition covers referencing/reusing something from earlier")
    print("=" * 60)
    text = " ".join(intent.SYSTEM_PROMPT.lower().split())

    # The narrow "adjust the last thing" examples must still be there.
    assert "make it more premium" in text, "FAIL: the original in-place-adjustment example regressed"

    # The new "reference/reuse something earlier" coverage must be present.
    assert "earlier" in text, "FAIL: expected explicit coverage of referring to something earlier in the conversation"
    assert "the one from before" in text or "last time" in text, (
        "FAIL: expected an explicit example of reusing/referencing a prior creative"
    )
    assert "second one" in text, "FAIL: expected an explicit example of pointing at a specific past item"
    assert "prompt" in text, "FAIL: expected explicit coverage of reusing a prior PROMPT, not just a prior image"
    print("PASS: REVISE now explicitly covers both in-place adjustment and reference/reuse phrasing\n")

    print("=" * 60)
    print("TEST 2: REVISE no longer requires an explicit change verb")
    print("=" * 60)
    assert "even without an explicit change described" in text, (
        "FAIL: expected the classifier to be told a bare reference (no described change) still counts as REVISE"
    )
    print("PASS\n")


def run():
    test_revise_covers_reference_and_reuse_phrasing()
    print("ALL TESTS PASSED")


run()
