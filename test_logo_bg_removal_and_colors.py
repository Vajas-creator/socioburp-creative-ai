"""
Test for two Aug 2026 follow-up fixes surfaced by a real logo-upload
screenshot: a white/light box visibly overlapping the generated creative
where the logo should sit, and generated images defaulting to the wrong
dominant color despite the client's actual logo being white/sky-blue.

Root causes (see app/engine/logo_bg_removal.py and app/engine/
logo_capture.py's module docstrings for the full story):
  1. A logo uploaded on a plain white background arrives with no real
     alpha transparency (WhatsApp re-encodes as JPEG, which has none at
     all) -- compositor.py's paste showed the ENTIRE bounding rectangle,
     background color included, as a solid block. app/engine/
     logo_bg_removal.py flood-fills a uniform background out to
     transparency, once, at upload time.
  2. app/engine/color_discovery.py (vision-based brand color extraction)
     already existed and was used during onboarding's Instagram-
     screenshot step, but was never wired into the "upload/update logo
     mid-conversation" path -- so a business that onboarded before
     sending their real logo never got its actual colors applied. Now
     every logo upload also triggers a color read, auto-applied if
     confident.

Covers:
  - logo_bg_removal.remove_uniform_background(): a logo on a uniform
    background gets its background keyed to transparency while the
    actual mark stays opaque; a genuinely solid-color image (no separate
    background to find) is left untouched rather than erased entirely;
    a busy/non-uniform-cornered image is left untouched; never raises.
  - logo_capture.handle(): the bytes handed to upload_logo() have gone
    through background removal; a confident color read updates
    BrandProfile.primary_color/secondary_color and is mentioned in the
    confirmation reply; an unconfident/failed color read leaves existing
    colors untouched and doesn't block saving the logo; a fresh logo
    upload's colors OVERWRITE previously-set colors (the logo is treated
    as the most authoritative brand signal).
"""
import sys
import asyncio
import os
import io

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_logo_bg_removal_and_colors.db"
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("R2_ACCOUNT_ID", "fake")
os.environ.setdefault("R2_ACCESS_KEY", "fake")
os.environ.setdefault("R2_SECRET_KEY", "fake")
os.environ.setdefault("R2_BUCKET", "fake")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "https://fake.example.com")

from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


from app import db as db_module  # noqa: E402
import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from PIL import Image, ImageDraw  # noqa: E402
from app.engine import logo_bg_removal, logo_capture, color_discovery  # noqa: E402


def _logo_on_white(mark_color=(135, 206, 235), size=(300, 300)):
    img = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse((75, 75, 225, 225), fill=mark_color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")  # simulate WhatsApp's alpha-stripping re-encode
    return buf.getvalue()


def test_remove_uniform_background():
    print("=" * 60)
    print("TEST 1: a logo on a uniform white background gets that background keyed to transparency")
    print("=" * 60)
    out = logo_bg_removal.remove_uniform_background(_logo_on_white())
    result = Image.open(io.BytesIO(out))
    assert result.mode == "RGBA"
    assert result.getpixel((2, 2))[3] == 0, "FAIL: expected the background corner to be transparent"
    assert result.getpixel((150, 150))[3] == 255, "FAIL: expected the logo mark itself to stay opaque"
    print("PASS: background transparent, mark opaque\n")

    print("=" * 60)
    print("TEST 2: a genuinely solid-color image (no separate background) is left untouched, not erased")
    print("=" * 60)
    solid = Image.new("RGB", (200, 200), (135, 206, 235))
    buf = io.BytesIO()
    solid.save(buf, format="PNG")
    out = logo_bg_removal.remove_uniform_background(buf.getvalue())
    result = Image.open(io.BytesIO(out))
    assert result.getpixel((100, 100))[3] == 255, (
        "FAIL: a solid-color logo with no real background got erased instead of left alone -- safety net failed"
    )
    print("PASS: solid-color image survives untouched\n")

    print("=" * 60)
    print("TEST 3: a non-uniform-cornered (busy/photo-style) image is left untouched")
    print("=" * 60)
    busy = Image.new("RGB", (200, 200))
    draw = ImageDraw.Draw(busy)
    draw.rectangle((0, 0, 99, 99), fill=(255, 0, 0))
    draw.rectangle((100, 0, 199, 99), fill=(0, 255, 0))
    draw.rectangle((0, 100, 99, 199), fill=(0, 0, 255))
    draw.rectangle((100, 100, 199, 199), fill=(255, 255, 0))
    buf = io.BytesIO()
    busy.save(buf, format="PNG")
    out = logo_bg_removal.remove_uniform_background(buf.getvalue())
    result = Image.open(io.BytesIO(out))
    assert result.getpixel((5, 5))[3] == 255 and result.getpixel((195, 195))[3] == 255, (
        "FAIL: expected a genuinely 4-different-cornered image to be left fully opaque"
    )
    print("PASS: non-uniform corners correctly left untouched\n")

    print("=" * 60)
    print("TEST 4: garbage bytes never raise -- fails safe to the original bytes")
    print("=" * 60)
    out = logo_bg_removal.remove_uniform_background(b"not an image at all")
    assert out == b"not an image at all"
    print("PASS: garbage input returned unmodified, no exception\n")


async def test_logo_capture_wires_bg_removal_and_colors():
    from app.db import get_session
    from app.models import Business, BrandProfile
    from app.schemas import IncomingMessage
    from app.whatsapp import client as wa_client

    sent = []

    async def fake_send_text(to, body):
        sent.append(body)

    wa_client.send_text = fake_send_text
    logo_capture.send_text = fake_send_text

    async def fake_download_media(media_id):
        return _logo_on_white()

    logo_capture.download_media = fake_download_media

    captured_upload_bytes = {}

    def fake_upload_logo(business_id, image_bytes):
        captured_upload_bytes["bytes"] = image_bytes
        return f"https://fake.example.com/logos/{business_id}.png"

    logo_capture.upload_logo = fake_upload_logo

    with get_session() as db:
        biz = Business(phone="919999999960", name="Test Biz", industry="tech", onboarding_state="done")
        db.add(biz)
        db.flush()
        biz_id = biz.id

    print("=" * 60)
    print("TEST 5: logo_capture.handle() passes background-REMOVED bytes to upload_logo(), not the raw upload")
    print("=" * 60)

    async def fake_extract_colors_confident(image_bytes, media_type="image/jpeg"):
        return {"primary_color": "#87CEEB", "secondary_color": "#FFFFFF", "confident": True}

    color_discovery.extract_colors_from_image = fake_extract_colors_confident

    msg = IncomingMessage(sender="919999999960", type="image", media_id="wamid_logo1", text="this is my logo")
    await logo_capture.handle(biz_id, msg)

    uploaded = Image.open(io.BytesIO(captured_upload_bytes["bytes"]))
    assert uploaded.mode == "RGBA" and uploaded.getpixel((2, 2))[3] == 0, (
        "FAIL: expected upload_logo() to receive background-removed (transparent-cornered) bytes"
    )
    print("PASS: upload_logo() received the cleaned, transparent-background bytes\n")

    print("=" * 60)
    print("TEST 6: a confident color read updates BrandProfile and is mentioned in the confirmation")
    print("=" * 60)
    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
        assert profile.primary_color == "#87CEEB", f"FAIL: expected primary_color set from the logo, got {profile.primary_color!r}"
        assert profile.secondary_color == "#FFFFFF", f"FAIL: expected secondary_color set from the logo, got {profile.secondary_color!r}"
    assert any("#87CEEB" in s for s in sent), f"FAIL: expected the confirmation to mention the picked-up color, got {sent}"
    print(f"PASS: {sent[-1]!r}\n")

    print("=" * 60)
    print("TEST 7: an unconfident color read leaves existing colors untouched, doesn't block saving the logo")
    print("=" * 60)
    sent.clear()

    async def fake_extract_colors_unconfident(image_bytes, media_type="image/jpeg"):
        return {"primary_color": None, "secondary_color": None, "confident": False}

    color_discovery.extract_colors_from_image = fake_extract_colors_unconfident

    msg2 = IncomingMessage(sender="919999999960", type="image", media_id="wamid_logo2", text="this is my logo")
    await logo_capture.handle(biz_id, msg2)

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
        assert profile.primary_color == "#87CEEB", (
            f"FAIL: an unconfident read should leave the existing color alone, got {profile.primary_color!r}"
        )
        assert profile.logo_url, "FAIL: the logo itself should still have been saved despite the unconfident color read"
    assert any("saved your logo" in s.lower() for s in sent), f"FAIL: expected the logo-saved confirmation regardless, got {sent}"
    print("PASS: unconfident color read didn't clobber existing colors or block the logo save\n")

    print("=" * 60)
    print("TEST 8: a color-extraction exception doesn't block saving the logo")
    print("=" * 60)
    sent.clear()

    async def fake_extract_colors_raises(image_bytes, media_type="image/jpeg"):
        raise RuntimeError("simulated API failure")

    color_discovery.extract_colors_from_image = fake_extract_colors_raises

    msg3 = IncomingMessage(sender="919999999960", type="image", media_id="wamid_logo3", text=None)
    await logo_capture.handle(biz_id, msg3)

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
        assert profile.logo_url, "FAIL: the logo should still be saved even if color extraction raises"
    assert any("saved your logo" in s.lower() for s in sent), f"FAIL: expected the logo-saved confirmation despite the color-extraction error, got {sent}"
    print("PASS: color-extraction failure is fully swallowed, logo save unaffected\n")

    print("=" * 60)
    print("TEST 9: a fresh logo upload's confident color read OVERWRITES a previously-set color")
    print("=" * 60)
    sent.clear()

    async def fake_extract_colors_different(image_bytes, media_type="image/jpeg"):
        return {"primary_color": "#FF5733", "secondary_color": None, "confident": True}

    color_discovery.extract_colors_from_image = fake_extract_colors_different

    msg4 = IncomingMessage(sender="919999999960", type="image", media_id="wamid_logo4", text="this is my new logo")
    await logo_capture.handle(biz_id, msg4)

    with get_session() as db:
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == biz_id).first()
        assert profile.primary_color == "#FF5733", (
            f"FAIL: a fresh logo upload's color should overwrite the old one, got {profile.primary_color!r}"
        )
    print("PASS: a new logo upload's colors take priority over the old ones\n")


async def run():
    test_remove_uniform_background()
    await test_logo_capture_wires_bg_removal_and_colors()
    print("ALL TESTS PASSED")


asyncio.run(run())
