"""
Test for app/engine/image_gen.py's locked output aspect ratio: every
generated image must ship at exactly 1229x1536 px (~4:5 portrait),
regardless of what size the provider's API actually generated -- OpenAI's
image API only accepts a fixed enum ("1024x1024", "1024x1536",
"1536x1024", "auto"), none of which is 1229x1536, so this is enforced by
image_gen._fit_to_target_size() as a post-processing step applied to
every candidate before generate_images() returns.

Covers:
  - A square source (what _generate_openai actually requests) is
    center-cropped to the target aspect, then upscaled to the exact
    target pixel dimensions -- no stretching (crop preserves the source's
    per-pixel proportions; only the final uniform resize changes scale).
  - Both the plain-generate and the edit (reference-image) paths produce
    correctly-sized output, since _fit_to_target_size is applied centrally
    in generate_images() regardless of which path produced the bytes.
  - Already-correctly-sized and other-aspect sources also land on the
    exact target size (robustness, not just the common case).
"""
import asyncio
import io
import os
import sys

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_image_aspect_ratio.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("IMAGE_API_KEY", "fake")

from PIL import Image  # noqa: E402

from app.engine import image_gen  # noqa: E402


def _make_png(width, height, color=(120, 60, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    return buf.getvalue()


async def run():
    print("=" * 60)
    print("TEST 1: square source (1024x1024, what the API is actually asked for) -> exact target size")
    print("=" * 60)
    square = _make_png(1024, 1024)
    fitted = image_gen._fit_to_target_size(square)
    img = Image.open(io.BytesIO(fitted))
    assert img.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT), (
        f"FAIL: expected {(image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)}, got {img.size}"
    )
    print(f"PASS: {img.size}\n")

    print("=" * 60)
    print("TEST 2: already-portrait source (e.g. a different provider's native 1024x1536) -> still exact target size")
    print("=" * 60)
    portrait = _make_png(1024, 1536)
    fitted = image_gen._fit_to_target_size(portrait)
    img = Image.open(io.BytesIO(fitted))
    assert img.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT), (
        f"FAIL: expected {(image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)}, got {img.size}"
    )
    print(f"PASS: {img.size}\n")

    print("=" * 60)
    print("TEST 3: landscape source -> still exact target size (robustness against any provider quirk)")
    print("=" * 60)
    landscape = _make_png(1536, 1024)
    fitted = image_gen._fit_to_target_size(landscape)
    img = Image.open(io.BytesIO(fitted))
    assert img.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT), (
        f"FAIL: expected {(image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)}, got {img.size}"
    )
    print(f"PASS: {img.size}\n")

    print("=" * 60)
    print("TEST 4: generate_images() applies the fit to every candidate, on both the generate and edit paths")
    print("=" * 60)

    async def fake_generate_openai(prompt, count):
        return [_make_png(1024, 1024) for _ in range(count)]

    async def fake_edit_openai(prompt, count, reference_image):
        return [_make_png(1024, 1024) for _ in range(count)]

    image_gen._generate_openai = fake_generate_openai
    image_gen._edit_openai = fake_edit_openai

    results = await image_gen.generate_images("a bakery post", count=2)
    assert len(results) == 2
    for r in results:
        img = Image.open(io.BytesIO(r))
        assert img.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT), f"FAIL: generate path, got {img.size}"
    print(f"PASS: plain-generate path -> {[Image.open(io.BytesIO(r)).size for r in results]}")

    results = await image_gen.generate_images("edit this photo", count=2, reference_image=b"FAKE-PHOTO")
    assert len(results) == 2
    for r in results:
        img = Image.open(io.BytesIO(r))
        assert img.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT), f"FAIL: edit path, got {img.size}"
    print(f"PASS: edit (reference-image) path -> {[Image.open(io.BytesIO(r)).size for r in results]}\n")

    print("=" * 60)
    print("TEST 5: target ratio is ~4:5 as specified")
    print("=" * 60)
    ratio = image_gen.TARGET_WIDTH / image_gen.TARGET_HEIGHT
    assert image_gen.TARGET_WIDTH == 1229 and image_gen.TARGET_HEIGHT == 1536, (
        f"FAIL: expected the locked 1229x1536, got {image_gen.TARGET_WIDTH}x{image_gen.TARGET_HEIGHT}"
    )
    assert abs(ratio - 0.8) < 0.01, f"FAIL: expected ~4:5 (0.8), got {ratio}"
    print(f"PASS: {image_gen.TARGET_WIDTH}x{image_gen.TARGET_HEIGHT}, ratio={ratio:.4f} (~4:5)\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
