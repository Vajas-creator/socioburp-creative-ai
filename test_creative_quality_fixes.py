"""
Test for the Aug 2026 "product detailing + cropped text + robotic caption"
feedback round:

  - app/engine/quality.py: the quality gate never actually checked for
    cropped/cut-off text or product before this -- a candidate could score
    well on the other three criteria and still ship with a clipped
    headline, which is exactly what kept happening across THREE prior
    rounds of prompt-only tightening in prompt_builder.py. This adds an
    explicit, score-capping check as a backstop: prompt wording is still
    the first line of defense, but it's no longer the only one.
  - app/engine/prompt_builder.py: an explicit product/subject sharpness
    instruction, plus the safe-zone rule now references the new backstop.
  - app/engine/caption.py: captions should read like a real person texting
    a customer, not formal structured ad copy -- rewrote the style
    instruction away from the old "Hook line / 2-3 lines / CTA" template.

These are prompt-text changes -- there's no code branch to unit-test the
model's actual behavior against, so this locks in the INSTRUCTIONS
themselves (the same pattern test_carousel_no_collage.py's TEST 3 uses for
prompt_builder.py's anti-collage rule) so a future edit can't silently
regress any of these fixes.
"""
import sys
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_creative_quality_fixes.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

from app.engine import quality, prompt_builder, caption  # noqa: E402


def test_quality_gate_checks_for_cropping():
    print("=" * 60)
    print("TEST 1: quality.py's SYSTEM_PROMPT explicitly checks for cut-off/cropped content")
    print("=" * 60)
    text = quality.SYSTEM_PROMPT.lower()
    assert "cut off" in text or "cropped" in text, "FAIL: expected an explicit cropping/cut-off check"
    assert "edge" in text, "FAIL: expected the check to reference image edges"
    assert "40" in quality.SYSTEM_PROMPT, "FAIL: expected the score-capping value (40) to be stated"
    assert "cap" in text or "cannot exceed" in text, "FAIL: expected this to be a hard cap, not just a point deduction"
    print("PASS: cropping/cut-off is an explicit, score-capping check\n")

    print("=" * 60)
    print("TEST 2: quality.py's SYSTEM_PROMPT checks for product/subject sharpness")
    print("=" * 60)
    assert "sharp" in text, "FAIL: expected a sharpness criterion"
    assert "detail" in text, "FAIL: expected a detail criterion"
    print("PASS: sharp/detailed product rendering is now scored\n")


def test_prompt_builder_product_detail_instruction():
    print("=" * 60)
    print("TEST 3: prompt_builder.py's SYSTEM_PROMPT instructs sharp/detailed product rendering")
    print("=" * 60)
    text = prompt_builder.SYSTEM_PROMPT.lower()
    assert "sharp" in text, "FAIL: expected a sharpness instruction for the product/subject"
    assert "detail" in text, "FAIL: expected a detail instruction for the product/subject"
    print("PASS\n")

    print("=" * 60)
    print("TEST 4: the anti-collage rule from earlier this session is still intact")
    print("=" * 60)
    assert "collage" in text, "FAIL: the anti-collage rule regressed"
    assert "1/5" in prompt_builder.SYSTEM_PROMPT or "progress bar" in text or "page/slide indicator" in text, (
        "FAIL: the anti-slide-indicator rule regressed"
    )
    print("PASS: earlier fixes weren't clobbered by this edit\n")


def test_caption_reads_like_texting_not_ad_copy():
    print("=" * 60)
    print("TEST 5: caption.py's SYSTEM_PROMPT asks for a texting style, not the old Hook/CTA template")
    print("=" * 60)
    text = caption.SYSTEM_PROMPT.lower()
    assert "hook line" not in text, "FAIL: the old formal Hook-line template should be gone"
    assert "texting" in text, "FAIL: expected explicit 'texting' framing"
    assert "40 words" in text, "FAIL: expected a short word-count ceiling for the caption body"
    assert "ad copy" in text or "ad brief" in text, "FAIL: expected an explicit contrast against formal ad copy"
    print("PASS: caption style now targets natural texting, not structured ad copy\n")


def run():
    test_quality_gate_checks_for_cropping()
    test_prompt_builder_product_detail_instruction()
    test_caption_reads_like_texting_not_ad_copy()
    print("ALL TESTS PASSED")


run()
