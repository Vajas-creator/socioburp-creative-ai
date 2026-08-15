"""
Test for app/engine/prompt_builder.py's subtext_text/cta_text fields --
Aug 2026 follow-up to the "text still cut" fix. A real production brief
asked for a bold headline PLUS a smaller subtext line PLUS a small
CTA/website line all on one image; the prior version of build() only ever
returned a single headline_text, so the rest of that content was silently
dropped (and, per the incident that prompted this, the conflict between
prompt_builder's "NO TEXT AT ALL" image-model rule and a brief explicitly
asking for four lines of on-image text was producing malformed
non-JSON responses that fell through to the generic fallback prompt).

Covers:
  - build() passes through subtext_text/cta_text when the model returns
    them.
  - build() defaults missing subtext_text/cta_text to "" rather than
    raising -- these are optional fields, only headline_text is required.
  - The exception-path fallback dict also includes subtext_text/cta_text
    as "", so callers can always safely do built.get("subtext_text").
  - SYSTEM_PROMPT documents all three fields and is still internally
    consistent with the "NO TEXT ON THE IMAGE" rule (i.e. still tells the
    model these are separate JSON fields, not part of image_prompt).
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_prompt_builder_multitext.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

from app.engine import prompt_builder  # noqa: E402
from app.engine.context import BusinessContext  # noqa: E402

ctx = BusinessContext(name="Copper & Crumb Bakery", industry="bakery")


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeContent(text)]


async def run():
    print("=" * 60)
    print("TEST 1: build() passes through subtext_text/cta_text when the model returns them")
    print("=" * 60)

    async def fake_create_message_full(**kwargs):
        return _FakeResponse(
            '{"image_prompt": "a bakery scene", "headline_text": "How SocioBurp Works", '
            '"subtext_text": "The AI Growth Platform that gets you discovered.", '
            '"cta_text": "DM us SYSTEM or visit www.socioburp.com", "notes_for_caption": "n/a"}'
        )

    prompt_builder.create_message = fake_create_message_full
    result = await prompt_builder.build(ctx, "carousel slide 1")
    assert result["headline_text"] == "How SocioBurp Works"
    assert result["subtext_text"] == "The AI Growth Platform that gets you discovered."
    assert result["cta_text"] == "DM us SYSTEM or visit www.socioburp.com"
    print("PASS: all three text fields passed through correctly\n")

    print("=" * 60)
    print("TEST 2: build() defaults missing subtext_text/cta_text to \"\" rather than raising")
    print("=" * 60)

    async def fake_create_message_headline_only(**kwargs):
        return _FakeResponse(
            '{"image_prompt": "a bakery scene", "headline_text": "Big Sale", "notes_for_caption": "n/a"}'
        )

    prompt_builder.create_message = fake_create_message_headline_only
    result = await prompt_builder.build(ctx, "a simple sale post")
    assert result["headline_text"] == "Big Sale"
    assert result["subtext_text"] == "", f"FAIL: expected subtext_text to default to '', got {result.get('subtext_text')!r}"
    assert result["cta_text"] == "", f"FAIL: expected cta_text to default to '', got {result.get('cta_text')!r}"
    print("PASS: missing optional fields default to '' instead of raising\n")

    print("=" * 60)
    print("TEST 3: exception-path fallback also includes subtext_text/cta_text as \"\"")
    print("=" * 60)

    async def fake_create_message_malformed(**kwargs):
        return _FakeResponse("This is not JSON at all, sorry, I can't do that.")

    prompt_builder.create_message = fake_create_message_malformed
    result = await prompt_builder.build(ctx, "some request that confused the model")
    assert "subtext_text" in result and result["subtext_text"] == ""
    assert "cta_text" in result and result["cta_text"] == ""
    assert result["headline_text"], "FAIL: fallback should still provide a non-empty headline_text"
    assert result["image_prompt"], "FAIL: fallback should still provide a non-empty image_prompt"
    print("PASS: fallback dict has the full expected shape, callers can always built.get('subtext_text') safely\n")

    print("=" * 60)
    print("TEST 4: SYSTEM_PROMPT documents all three text fields and keeps the NO-TEXT rule intact")
    print("=" * 60)
    text = prompt_builder.SYSTEM_PROMPT.lower()
    assert "subtext_text" in text and "cta_text" in text and "headline_text" in text
    assert "no text of any kind" in text, "FAIL: the core 'model paints no text' rule regressed"
    normalized = " ".join(text.split())
    assert "not part of image_prompt" in normalized, "FAIL: expected the three text fields to be documented as separate from image_prompt"
    print("PASS: SYSTEM_PROMPT still internally consistent\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
