"""
Test for app/engine/image_gen.py's AI-outpaint canvas extension -- Aug 2026
"my image and text still cut" deep-dive.

Previously, every generated image (native 1024x1536 from the provider) was
forced to the final 1229x1536 delivery size by CENTER-CROPPING roughly 8%
off the top and bottom -- destroying anything the model drew too close to
those edges, no matter how hard the prompt tried to keep it away. That crop
step is categorically incapable of ever being made safe: it's a delete
operation on real pixels.

This replaces that as the PRIMARY path with an EXTEND operation instead:
the source already matches the target height exactly, so the resize step
only needs to grow the width, and does so by outpainting new content into
freshly added margins via OpenAI's image-edit endpoint -- nothing that was
actually drawn is ever removed. The old crop-based fit is kept only as a
fallback for when the outpaint call itself fails, or the source shape is
unexpected -- strictly no worse than the previous behavior, never silently
broken.

Covers:
  - Source already at the exact target size -> passthrough, no outpaint
    call at all.
  - Native 1024x1536 source, outpaint succeeds -> the outpainted bytes are
    used (not a crop of the original).
  - Native 1024x1536 source, outpaint returns None (provider error) ->
    falls back to the crop-based fit, still lands on the exact target size.
  - Native 1024x1536 source, outpaint raises -> same safe fallback.
  - An unexpected source shape (not 1024x1536) -> skips the outpaint call
    entirely (the math assumes a pure width extension at matching height)
    and goes straight to the crop-based fallback.
  - _outpaint_openai() builds a canvas sized to the FINAL target dimensions
    with the source pasted centered, and a mask that marks exactly the
    source region as "preserve" (opaque) and the new margins as "generate"
    (transparent) -- gets this backwards and outpainting would either
    regenerate the entire photo or preserve nothing.
  - _outpaint_openai() returns None (not raises) on an HTTP error response,
    so the caller's fallback logic actually triggers.
  - generate_images() end-to-end: a real 1024x1536 PNG candidate from the
    (mocked) generate path gets extended via outpaint, landing at the
    exact target size using the outpainted content, not a crop.
"""
import sys
import asyncio
import io
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_image_outpaint.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("IMAGE_API_KEY", "fake")

from PIL import Image  # noqa: E402

from app.engine import image_gen  # noqa: E402

_REAL_OUTPAINT_OPENAI = image_gen._outpaint_openai


def _make_png(width, height, color=(120, 60, 200)):
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=color).save(buf, format="PNG")
    return buf.getvalue()


async def run():
    print("=" * 60)
    print("TEST 1: already at target size -> passthrough, outpaint never called")
    print("=" * 60)
    called = {"n": 0}

    async def fake_outpaint_should_not_run(img):
        called["n"] += 1
        return None

    image_gen._outpaint_openai = fake_outpaint_should_not_run
    src = _make_png(image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)
    out = await image_gen._extend_to_target_size(src)
    result = Image.open(io.BytesIO(out))
    assert result.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)
    assert called["n"] == 0, "FAIL: outpaint was called even though the source was already the target size"
    print(f"PASS: {result.size}, outpaint untouched\n")

    print("=" * 60)
    print("TEST 2: native 1024x1536, outpaint succeeds -> outpainted bytes used, not a crop")
    print("=" * 60)
    native = _make_png(1024, 1536, color=(0, 255, 0))
    outpainted_marker = _make_png(image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT, color=(0, 0, 255))

    async def fake_outpaint_success(img):
        called["n"] += 1
        return outpainted_marker

    called["n"] = 0
    image_gen._outpaint_openai = fake_outpaint_success
    out = await image_gen._extend_to_target_size(native)
    result = Image.open(io.BytesIO(out))
    assert result.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)
    assert called["n"] == 1
    px = result.getpixel((5, 5))
    assert px[2] > 200 and px[1] < 80, f"FAIL: expected the outpainted (blue) marker, got {px} -- looks like a crop of the green original instead"
    print(f"PASS: {result.size}, used outpainted content ({px})\n")

    print("=" * 60)
    print("TEST 3: native 1024x1536, outpaint returns None -> falls back to crop-based fit")
    print("=" * 60)
    called["n"] = 0

    async def fake_outpaint_fails(img):
        called["n"] += 1
        return None

    image_gen._outpaint_openai = fake_outpaint_fails
    out = await image_gen._extend_to_target_size(native)
    result = Image.open(io.BytesIO(out))
    assert result.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)
    assert called["n"] == 1
    px = result.getpixel((5, 5))
    assert px[1] > 200, f"FAIL: expected the cropped-and-upscaled green original as fallback, got {px}"
    print(f"PASS: {result.size}, fell back to crop-based fit ({px})\n")

    print("=" * 60)
    print("TEST 4: native 1024x1536, outpaint raises -> same safe fallback, no crash")
    print("=" * 60)

    async def fake_outpaint_raises(img):
        raise RuntimeError("provider timeout")

    image_gen._outpaint_openai = fake_outpaint_raises
    out = await image_gen._extend_to_target_size(native)
    result = Image.open(io.BytesIO(out))
    assert result.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)
    print(f"PASS: {result.size}, exception swallowed, safe fallback used\n")

    print("=" * 60)
    print("TEST 5: unexpected source shape -> outpaint skipped entirely, straight to crop fallback")
    print("=" * 60)
    called["n"] = 0
    image_gen._outpaint_openai = fake_outpaint_fails  # would increment called['n'] if invoked
    weird_shape = _make_png(800, 1200, color=(255, 255, 0))
    out = await image_gen._extend_to_target_size(weird_shape)
    result = Image.open(io.BytesIO(out))
    assert result.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)
    assert called["n"] == 0, "FAIL: outpaint was called for a shape its math doesn't support"
    print(f"PASS: {result.size}, outpaint correctly skipped for a non-1024x1536 source\n")

    print("=" * 60)
    print("TEST 6: _outpaint_openai builds a correctly-sized canvas + mask, posts to the edits endpoint")
    print("=" * 60)
    import base64
    import httpx

    # TESTs 1-5 above all replaced image_gen._outpaint_openai with fakes to
    # test _extend_to_target_size()'s fallback behavior in isolation -- the
    # module-level name is left pointing at whichever fake ran last. TESTs
    # 6/7 below test the REAL _outpaint_openai() implementation directly,
    # so it must be restored first, or they'd silently test a stub.
    image_gen._outpaint_openai = _REAL_OUTPAINT_OPENAI

    captured = {}

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"b64_json": base64.b64encode(
                _make_png(image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)
            ).decode()}]}

    async def fake_post(self, url, headers=None, files=None, data=None, **kwargs):
        captured["url"] = url
        captured["canvas"] = Image.open(io.BytesIO(files["image"][1]))
        captured["mask"] = Image.open(io.BytesIO(files["mask"][1])).convert("RGBA")
        captured["prompt"] = data["prompt"]
        return _FakeResponse()

    real_post = httpx.AsyncClient.post
    httpx.AsyncClient.post = fake_post
    try:
        src_img = Image.new("RGB", (1024, 1536), (10, 20, 30))
        result_bytes = await image_gen._outpaint_openai(src_img)
    finally:
        httpx.AsyncClient.post = real_post

    assert captured["url"] == "https://api.openai.com/v1/images/edits"
    assert captured["canvas"].size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT), (
        f"FAIL: canvas must be built at the FINAL target size so the source can be pasted centered into it, got {captured['canvas'].size}"
    )
    assert captured["mask"].size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)

    paste_x = (image_gen.TARGET_WIDTH - 1024) // 2
    mid_y = image_gen.TARGET_HEIGHT // 2
    # Inside the pasted source region -> mask must be OPAQUE (preserve).
    inside_alpha = captured["mask"].getpixel((paste_x + 5, mid_y))[3]
    assert inside_alpha == 255, f"FAIL: mask over the preserved source region must be fully opaque, got alpha={inside_alpha}"
    # In the new left margin -> mask must be TRANSPARENT (generate here).
    margin_alpha = captured["mask"].getpixel((5, mid_y))[3]
    assert margin_alpha == 0, f"FAIL: mask over the new margin (where outpainting should happen) must be transparent, got alpha={margin_alpha}"
    assert "no" in captured["prompt"].lower() and "text" in captured["prompt"].lower(), (
        "FAIL: outpaint prompt should explicitly forbid adding new text/logos/subjects into the extended margins"
    )
    assert result_bytes is not None
    result_img = Image.open(io.BytesIO(result_bytes))
    assert result_img.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)
    print("PASS: canvas at target size, source pasted centered, mask correctly marks preserve-vs-generate regions\n")

    print("=" * 60)
    print("TEST 7: _outpaint_openai returns None (not raise) on an HTTP error response")
    print("=" * 60)

    class _FakeErrorResponse:
        status_code = 400
        text = "bad request"

    async def fake_post_error(self, url, headers=None, files=None, data=None, **kwargs):
        return _FakeErrorResponse()

    httpx.AsyncClient.post = fake_post_error
    try:
        result = await image_gen._outpaint_openai(Image.new("RGB", (1024, 1536), (1, 2, 3)))
    finally:
        httpx.AsyncClient.post = real_post
    assert result is None, "FAIL: an HTTP error response must yield None so the caller's fallback logic runs, not raise"
    print("PASS: HTTP error -> None, no exception escapes\n")

    print("=" * 60)
    print("TEST 8: generate_images() end-to-end -- real 1024x1536 candidate gets extended via outpaint")
    print("=" * 60)

    async def fake_generate_openai(prompt, count):
        return [_make_png(1024, 1536, color=(200, 10, 10)) for _ in range(count)]

    image_gen._generate_openai = fake_generate_openai
    outpainted_marker2 = _make_png(image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT, color=(10, 10, 200))
    called["n"] = 0

    async def fake_outpaint_success2(img):
        called["n"] += 1
        return outpainted_marker2

    image_gen._outpaint_openai = fake_outpaint_success2
    results = await image_gen.generate_images("a bakery post", count=2)
    assert len(results) == 2
    assert called["n"] == 2, f"FAIL: expected outpaint called once per candidate, got {called['n']}"
    for r in results:
        img = Image.open(io.BytesIO(r))
        assert img.size == (image_gen.TARGET_WIDTH, image_gen.TARGET_HEIGHT)
        px = img.getpixel((5, 5))
        assert px[2] > 150 and px[0] < 80, f"FAIL: expected outpainted (blue) content in the final result, got {px}"
    print(f"PASS: generate_images() extends every candidate via outpaint -> {[Image.open(io.BytesIO(r)).size for r in results]}\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
