"""
Test for app/engine/image_gen.py's locked output aspect ratio: every
generated image must ship at exactly 1229x1536 px (~4:5 portrait),
regardless of what size the provider's API actually generated -- OpenAI's
image API only accepts a fixed enum ("1024x1024", "1024x1536",
"1536x1024", "auto"), none of which is 1229x1536, so this is enforced by
image_gen._fit_to_target_size() as a post-processing step applied to
every candidate before generate_images() returns.

_generate_openai/_edit_openai request "1024x1536" (native portrait), NOT
square -- this was an actual production incident (Aug 2026): requesting
square meant the crop had to remove ~20% off the LEFT and RIGHT to reach
the ~4:5 target, clipping headline text that extended into that margin
("Treat Yourselves" rendered as "reat Yourselves"). Portrait is close
enough to the target ratio that the crop only touches the TOP/BOTTOM
(~8% each), leaving the full width -- where headline text actually lives
-- untouched. TEST 2 below is a regression guard against reintroducing
the square request.

Covers:
  - A portrait source (1024x1536, what _generate_openai/_edit_openai
    actually request) crops height-only -- full width preserved -- then
    upscales to the exact target pixel dimensions, no stretching.
  - Both the plain-generate and the edit (reference-image) paths produce
    correctly-sized output, since _fit_to_target_size is applied centrally
    in generate_images() regardless of which path produced the bytes.
  - Square and landscape sources also land on the exact target size
    (robustness against any provider quirk), but portrait is the one that
    matters for headline-text safety and is what's actually requested.
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
    print("TEST 1: square source (defensive robustness only -- NOT what's actually requested anymore)")
    print("=" * 60)
    square = _make_png(1024, 1024)
    fitted = image_gen._fit_to_target_size(square)
    img = Image.open(io.BytesIO(fitted))
    assert img.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT), (
        f"FAIL: expected {(image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)}, got {img.size}"
    )
    print(f"PASS: {img.size}\n")

    print("=" * 60)
    print("TEST 2: portrait source (1024x1536, what _generate_openai/_edit_openai actually request) --")
    print("crop must be height-only; content at the LEFT/RIGHT edges must survive (regression guard")
    print("for the headline-clipping incident: a square source needed a ~20%-of-width crop and clipped")
    print("headline text sitting near the edges -- 'Treat Yourselves' rendered as 'reat Yourselves')")
    print("=" * 60)
    portrait_w, portrait_h = 1024, 1536
    img = Image.new("RGB", (portrait_w, portrait_h), color=(120, 60, 200))
    # Simulate headline-ish content sitting right at the extreme left/right
    # edges, at a vertical position well inside the safe zone -- a 10px
    # marker strip of a distinct color at column 0 and at the last column.
    for y in range(portrait_h):
        img.putpixel((0, y), (255, 0, 0))    # left-edge marker: pure red
        img.putpixel((portrait_w - 1, y), (0, 0, 255))  # right-edge marker: pure blue
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    portrait = buf.getvalue()

    fitted = image_gen._fit_to_target_size(portrait)
    result = Image.open(io.BytesIO(fitted))
    assert result.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT), (
        f"FAIL: expected {(image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)}, got {result.size}"
    )
    mid_y = result.height // 2
    left_pixel = result.getpixel((0, mid_y))
    right_pixel = result.getpixel((result.width - 1, mid_y))
    assert left_pixel[0] > 200 and left_pixel[2] < 80, (
        f"FAIL: left-edge marker was cropped away (width got cut) -- got pixel {left_pixel}, expected red-ish"
    )
    assert right_pixel[2] > 200 and right_pixel[0] < 80, (
        f"FAIL: right-edge marker was cropped away (width got cut) -- got pixel {right_pixel}, expected blue-ish"
    )
    print(f"PASS: {result.size}, edge markers survived (left={left_pixel}, right={right_pixel}) -- width was never touched\n")

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

    # Save the real functions -- TEST 6 below needs them (TEST 4 stubs
    # these out permanently on the module, and never restoring them would
    # make TEST 6 silently test its own stub instead of the real thing).
    real_generate_openai = image_gen._generate_openai
    real_edit_openai = image_gen._edit_openai

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

    print("=" * 60)
    print("TEST 6: the ACTUAL OpenAI request asks for 1024x1536, not square (this is the real fix --")
    print("_fit_to_target_size can only do so much; the request itself must not ask for square)")
    print("=" * 60)
    import httpx

    image_gen._generate_openai = real_generate_openai
    image_gen._edit_openai = real_edit_openai

    captured = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            import base64
            return {"data": [{"b64_json": base64.b64encode(_make_png(1024, 1536)).decode()}]}

    async def fake_post_json(self, url, headers=None, json=None, **kwargs):
        captured["generate_size"] = json["size"]
        return _FakeResponse()

    async def fake_post_multipart(self, url, headers=None, files=None, data=None, **kwargs):
        captured["edit_size"] = data["size"]
        return _FakeResponse()

    real_post = httpx.AsyncClient.post
    httpx.AsyncClient.post = fake_post_json
    await image_gen._generate_openai("a bakery post", count=1)
    assert captured.get("generate_size") == "1024x1536", (
        f"FAIL: _generate_openai must request '1024x1536', not square -- got {captured.get('generate_size')!r}"
    )
    print(f"PASS: _generate_openai requests size={captured['generate_size']!r}")

    httpx.AsyncClient.post = fake_post_multipart
    await image_gen._edit_openai("edit this", count=1, reference_image=b"FAKE")
    httpx.AsyncClient.post = real_post
    assert captured.get("edit_size") == "1024x1536", (
        f"FAIL: _edit_openai must request '1024x1536', not square -- got {captured.get('edit_size')!r}"
    )
    print(f"PASS: _edit_openai requests size={captured['edit_size']!r}\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
