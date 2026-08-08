"""
Test for app/engine/image_gen.py's reference-image (edit) support.

Previously generate_images() only ever called OpenAI's text-to-image
/v1/images/generations endpoint -- an uploaded product photo was never
usable as a starting point, only ever generating an unrelated brand-new
image from a text description. This adds an edit path (_edit_openai,
/v1/images/edits) used when a reference_image is given, with a safe
fallback to the existing text-to-image path if the edit call fails for
any reason -- never worse than today's behavior, only better when it works.

Covers:
  - No reference_image -> goes straight to the existing generate path
    (no change in behavior for pure text requests).
  - A reference_image -> calls the edit path, not generate.
  - Edit path failing (e.g. provider error) -> falls back to the generate
    path rather than returning nothing.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_image_gen_edit.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("IMAGE_API_KEY", "fake")

from app.engine import image_gen  # noqa: E402

calls = {"generate": 0, "edit": 0}


async def fake_generate_openai(prompt, count):
    calls["generate"] += 1
    return [b"GENERATED"] * count


async def run():
    print("=" * 60)
    print("TEST 1: no reference_image -> plain text-to-image path, unchanged")
    print("=" * 60)
    calls["generate"] = calls["edit"] = 0
    image_gen._generate_openai = fake_generate_openai

    result = await image_gen.generate_images("a bakery post", count=2)

    assert calls["generate"] == 1 and calls["edit"] == 0, f"FAIL: expected only the generate path, got {calls}"
    assert result == [b"GENERATED", b"GENERATED"]
    print("PASS: text-only request never touches the edit path\n")

    print("=" * 60)
    print("TEST 2: reference_image given -> edit path used, not generate")
    print("=" * 60)
    calls["generate"] = calls["edit"] = 0

    async def fake_edit_openai(prompt, count, reference_image):
        calls["edit"] += 1
        assert reference_image == b"PRODUCT-PHOTO"
        return [b"EDITED"] * count

    image_gen._edit_openai = fake_edit_openai

    result = await image_gen.generate_images("change background to black", count=2, reference_image=b"PRODUCT-PHOTO")

    assert calls["edit"] == 1 and calls["generate"] == 0, f"FAIL: expected only the edit path, got {calls}"
    assert result == [b"EDITED", b"EDITED"]
    print("PASS: a reference image routes to the edit endpoint\n")

    print("=" * 60)
    print("TEST 3: edit path fails/returns nothing -> falls back to generate, not empty")
    print("=" * 60)
    calls["generate"] = calls["edit"] = 0

    async def fake_edit_openai_fails(prompt, count, reference_image):
        calls["edit"] += 1
        return []  # provider error, nothing usable came back

    image_gen._edit_openai = fake_edit_openai_fails

    result = await image_gen.generate_images("change background to black", count=2, reference_image=b"PRODUCT-PHOTO")

    assert calls["edit"] == 1 and calls["generate"] == 1, f"FAIL: expected edit attempted then generate as fallback, got {calls}"
    assert result == [b"GENERATED", b"GENERATED"], f"FAIL: expected the fallback result, got {result}"
    print("PASS: a failed edit falls back to plain generation instead of failing the whole request\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
