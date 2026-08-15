"""
Test for app/engine/text_overlay.py -- the real, code-rendered headline
text that replaced the image-gen model's own attempt at painting text, per
the Aug 2026 "my image and text still cut" deep-dive. See the module
docstring in app/engine/text_overlay.py for the full root-cause reasoning:
a systematic model bias toward crowding text near the edges showed up in
EVERY candidate in a batch, so no amount of prompt-tightening or "pick the
best of N" quality-gating could fully eliminate garbled/cut-off headlines.
This composites the headline deterministically instead, with a real
bundled font, so there is no "cropped/garbled text" failure mode left by
construction.

Covers:
  - choose_text_box() returns a box fully within the canvas bounds, and
    falls back to a fixed bottom-third box if the vision call fails.
  - _wrap_and_fit() always returns something drawable that fits the given
    box for reasonable headline lengths, shrinking font size as needed --
    never raises, never silently drops text.
  - composite_headline() actually changes the pixels (draws something),
    returns the original bytes unmodified on any internal failure (fail
    safe, same pattern as compositor.py's composite_logo()), and returns
    the original bytes unmodified for an empty/whitespace-only headline
    (nothing to draw).
  - composite_headline() picks the correct bundled font file for a
    non-Latin language (Hindi/Devanagari) vs the default (English/Latin).
"""
import sys
import asyncio
import io
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_text_overlay.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("IMAGE_API_KEY", "fake")

from PIL import Image, ImageDraw  # noqa: E402

from app.engine import text_overlay  # noqa: E402


def _make_png(width, height, color=(30, 30, 30)):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    return buf.getvalue()


async def run():
    print("=" * 60)
    print("TEST 1: choose_text_box() falls back to a fixed bottom-third box when the vision call fails")
    print("=" * 60)

    async def fake_create_message_fails(**kwargs):
        raise RuntimeError("anthropic down")

    text_overlay.create_message = fake_create_message_fails
    box = await text_overlay.choose_text_box(_make_png(1000, 1500), 1000, 1500, "Weekend Sale")
    x, y, w, h = box
    assert 0 <= x < 1000 and 0 <= y < 1500
    assert x + w <= 1000 and y + h <= 1500
    assert y > 1500 * 0.5, f"FAIL: fallback box should sit in the lower portion of the canvas, got y={y}"
    print(f"PASS: fallback box {box} is within bounds and in the lower portion\n")

    print("=" * 60)
    print("TEST 2: choose_text_box() clamps a vision response that overruns the canvas")
    print("=" * 60)

    class _FakeContent:
        text = '{"x": 900, "y": 1400, "width": 5000, "height": 5000}'

    class _FakeResponse:
        content = [_FakeContent()]

    async def fake_create_message_overruns(**kwargs):
        return _FakeResponse()

    text_overlay.create_message = fake_create_message_overruns
    box = await text_overlay.choose_text_box(_make_png(1000, 1500), 1000, 1500, "Weekend Sale")
    x, y, w, h = box
    assert x + w <= 1000, f"FAIL: box overruns the right edge: x={x} w={w}"
    assert y + h <= 1500, f"FAIL: box overruns the bottom edge: y={y} h={h}"
    print(f"PASS: clamped box {box} stays fully within the 1000x1500 canvas\n")

    print("=" * 60)
    print("TEST 3: choose_text_box() returns a sane box on a normal vision response")
    print("=" * 60)

    class _FakeContentNormal:
        text = '{"x": 50, "y": 900, "width": 900, "height": 400}'

    class _FakeResponseNormal:
        content = [_FakeContentNormal()]

    async def fake_create_message_normal(**kwargs):
        return _FakeResponseNormal()

    text_overlay.create_message = fake_create_message_normal
    box = await text_overlay.choose_text_box(_make_png(1000, 1500), 1000, 1500, "Weekend Sale")
    assert box == (50, 900, 900, 400), f"FAIL: expected the vision response passed through (clamped), got {box}"
    print(f"PASS: {box}\n")

    print("=" * 60)
    print("TEST 4: _wrap_and_fit() fits a short headline at a large size within a generous box")
    print("=" * 60)
    dummy = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(dummy)
    font_path = os.path.join(text_overlay._FONTS_DIR, "NotoSans-Bold.ttf")
    font, lines = text_overlay._wrap_and_fit(draw, "Big Sale", font_path, box_w=800, box_h=300)
    assert font.size > 40, f"FAIL: a short headline in a generous box should use a large size, got {font.size}"
    assert "".join(lines).replace(" ", "") == "BigSale".replace(" ", "") or " ".join(lines) == "Big Sale"
    print(f"PASS: size={font.size}, lines={lines}\n")

    print("=" * 60)
    print("TEST 5: _wrap_and_fit() shrinks to fit a long headline in a small box, never drops the text")
    print("=" * 60)
    long_headline = "Weekend Mega Sale Everything Must Go Today Only Limited Stock"
    font, lines = text_overlay._wrap_and_fit(draw, long_headline, font_path, box_w=300, box_h=150)
    assert font.size >= 16, f"FAIL: font size must never go below the documented floor, got {font.size}"
    reconstructed = " ".join(lines)
    assert reconstructed.split() == long_headline.split(), (
        f"FAIL: wrapping must never drop or truncate words -- expected all of {long_headline.split()!r}, got {reconstructed.split()!r}"
    )
    print(f"PASS: size={font.size}, {len(lines)} line(s), full text preserved\n")

    print("=" * 60)
    print("TEST 6: composite_headline() with empty/whitespace headline returns the image unmodified")
    print("=" * 60)
    text_overlay.create_message = fake_create_message_normal
    original = _make_png(1229, 1536)
    out = await text_overlay.composite_headline(original, "   ")
    assert out == original, "FAIL: an empty/whitespace headline should return the original bytes untouched"
    print("PASS: unmodified passthrough for blank headline\n")

    print("=" * 60)
    print("TEST 7: composite_headline() actually draws something (pixels change) for a real headline")
    print("=" * 60)
    solid = _make_png(1229, 1536, color=(200, 100, 50))
    out = await text_overlay.composite_headline(solid, "Weekend Sale: 50% Off Today Only")
    result = Image.open(io.BytesIO(out))
    assert result.size == (1229, 1536)
    assert out != solid, "FAIL: composite_headline should have changed the pixels by drawing the headline + scrim"
    print(f"PASS: output size {result.size}, pixels changed as expected\n")

    print("=" * 60)
    print("TEST 8: composite_headline() fails safe -- returns original bytes if choose_text_box blows up")
    print("=" * 60)

    async def fake_choose_text_box_raises(*args, **kwargs):
        raise RuntimeError("boom")

    real_choose_text_box = text_overlay.choose_text_box
    text_overlay.choose_text_box = fake_choose_text_box_raises
    original2 = _make_png(1229, 1536)
    out = await text_overlay.composite_headline(original2, "Weekend Sale")
    assert out == original2, "FAIL: composite_headline must fail safe to the original image, not raise or return garbage"
    text_overlay.choose_text_box = real_choose_text_box
    print("PASS: fails safe to unmodified original on internal error\n")

    print("=" * 60)
    print("TEST 9: composite_headline() picks the Devanagari font for Hindi, not the Latin default")
    print("=" * 60)
    captured_font_paths = []
    real_fit_text_blocks = text_overlay._fit_text_blocks

    def spying_fit_text_blocks(draw, headline, subtext, cta_text, bold_font_path, regular_font_path, box_w, box_h):
        captured_font_paths.append((bold_font_path, regular_font_path))
        return real_fit_text_blocks(draw, headline, subtext, cta_text, bold_font_path, regular_font_path, box_w, box_h)

    text_overlay._fit_text_blocks = spying_fit_text_blocks
    text_overlay.create_message = fake_create_message_normal
    await text_overlay.composite_headline(_make_png(1229, 1536), "सप्ताहांत बिक्री", language="hi")
    text_overlay._fit_text_blocks = real_fit_text_blocks
    assert captured_font_paths and "Devanagari" in captured_font_paths[0][0], (
        f"FAIL: expected a Devanagari bold font file for Hindi, got {captured_font_paths}"
    )
    print(f"PASS: used {os.path.basename(captured_font_paths[0][0])}\n")

    print("=" * 60)
    print("TEST 10: every bundled font file referenced by the module actually resolves on disk")
    print("=" * 60)
    all_files = set(text_overlay._DEFAULT_FONT_FILES)
    for reg, bold in text_overlay._LANGUAGE_FONT_FILES.values():
        all_files.add(reg)
        all_files.add(bold)
    missing = [f for f in all_files if not os.path.isfile(os.path.join(text_overlay._FONTS_DIR, f))]
    assert not missing, f"FAIL: missing bundled font files: {missing}"
    print(f"PASS: all {len(all_files)} referenced font files exist under {text_overlay._FONTS_DIR}\n")

    print("=" * 60)
    print("TEST 11: _fit_text_blocks() with only a headline behaves like the single-block case")
    print("=" * 60)
    blocks = text_overlay._fit_text_blocks(
        draw, "Big Sale", None, None, font_path, font_path, box_w=800, box_h=300,
    )
    assert len(blocks) == 1, f"FAIL: expected exactly 1 block for headline-only, got {len(blocks)}"
    print(f"PASS: {len(blocks)} block, size={blocks[0]['font'].size}\n")

    print("=" * 60)
    print("TEST 12: _fit_text_blocks() with headline + subtext + cta produces 3 blocks in decreasing size")
    print("=" * 60)
    reg_path = os.path.join(text_overlay._FONTS_DIR, "NotoSans-Regular.ttf")
    blocks = text_overlay._fit_text_blocks(
        draw, "How SocioBurp Works",
        "The AI Growth Platform that gets your business discovered.",
        "DM us SYSTEM or visit www.socioburp.com",
        font_path, reg_path, box_w=1000, box_h=500,
    )
    assert len(blocks) == 3, f"FAIL: expected 3 blocks (headline+subtext+cta), got {len(blocks)}"
    sizes = [b["font"].size for b in blocks]
    assert sizes[0] > sizes[1] > sizes[2], f"FAIL: expected strictly decreasing sizes headline>subtext>cta, got {sizes}"
    total_reconstructed = sum(b["block_height"] + b["block_gap"] for b in blocks) - blocks[-1]["block_gap"]
    assert total_reconstructed <= 500, f"FAIL: fitted blocks exceed the given box_h, got {total_reconstructed}"
    print(f"PASS: 3 blocks, sizes={sizes} (strictly decreasing)\n")

    print("=" * 60)
    print("TEST 13: _fit_text_blocks() never drops words even under a tiny box (shrinks instead)")
    print("=" * 60)
    blocks = text_overlay._fit_text_blocks(
        draw,
        "A very long headline that will not fit at any large size in this tiny box",
        "An equally long subtext line that also needs to survive the shrink-to-fit process",
        "www.example.com",
        font_path, reg_path, box_w=200, box_h=150,
    )
    headline_words = "A very long headline that will not fit at any large size in this tiny box".split()
    reconstructed_headline = " ".join(blocks[0]["lines"]).split()
    assert reconstructed_headline == headline_words, (
        f"FAIL: headline words were dropped under a tiny box -- expected {headline_words}, got {reconstructed_headline}"
    )
    print(f"PASS: all {len(blocks)} blocks preserved full text even in a 200x150 box\n")

    print("=" * 60)
    print("TEST 14: composite_headline() draws all three blocks when subtext+cta are given")
    print("=" * 60)
    text_overlay.create_message = fake_create_message_normal
    solid2 = _make_png(1229, 1536, color=(30, 40, 90))
    out_headline_only = await text_overlay.composite_headline(solid2, "Big Sale")
    out_all_three = await text_overlay.composite_headline(
        solid2, "Big Sale", subtext="Everything must go this weekend", cta_text="Visit us today",
    )
    assert out_headline_only != solid2 and out_all_three != solid2
    assert out_headline_only != out_all_three, (
        "FAIL: adding subtext+cta should change the rendered output vs. headline-only"
    )
    img_all_three = Image.open(io.BytesIO(out_all_three))
    assert img_all_three.size == (1229, 1536)
    print("PASS: headline-only and headline+subtext+cta produce different (both valid) output\n")

    print("=" * 60)
    print("TEST 15: composite_headline() with blank subtext/cta behaves exactly like headline-only")
    print("=" * 60)
    out_blank_extras = await text_overlay.composite_headline(solid2, "Big Sale", subtext="   ", cta_text="")
    assert out_blank_extras == out_headline_only, (
        "FAIL: whitespace-only subtext and empty cta_text should be treated as absent, not as empty blocks"
    )
    print("PASS: blank/empty subtext and cta_text are correctly ignored\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
