"""
On-demand "customer simulation" QA tool -- built per Vajas's request for
something that "acts as a real customer" and reports issues "like an
expert with 25+ years of experience," instead of him having to manually
find problems by hand-testing on WhatsApp every time.

Different in kind from the test_*.py regression suite (see
.github/workflows/regression-tests.yml): those are free, fast, and
deterministic -- every Claude/image-gen call is mocked, and they assert
on CODE PATHS (did the right function get called, was a credit charged).
This instead runs the REAL pipeline end-to-end -- real Claude calls, real
OpenAI image generation -- against a throwaway database, with one Claude
"customer" persona driving a multi-turn conversation through
app.router.handle_message() (the exact same entry point a real WhatsApp
webhook hits), and a SEPARATE Claude "expert reviewer" (with vision, so it
actually looks at delivered images, not just reads text) critiquing the
whole transcript afterward.

On-demand only (workflow_dispatch in
.github/workflows/customer-simulation.yml) -- NOT scheduled, NOT run on
every push -- since every run costs real OpenAI/Anthropic money (several
image generations + many Claude calls per persona).

WhatsApp sends, WhatsApp media downloads, and R2 uploads are all stubbed
(captured/synthesized in-memory instead of hitting the real WhatsApp/R2
APIs) -- there's no real phone number or bucket on the other end of a
simulated run. But every module that actually touches Claude or the
image-gen API is left completely real and unmocked, since the whole
point is judging real model output quality, not code-path coverage.

Requires real ANTHROPIC_API_KEY and IMAGE_API_KEY env vars to do anything
useful -- see .github/workflows/customer-simulation.yml for the secrets
it expects. WA_*/R2_* can stay the same throwaway values the regression
suite uses, since those integrations are stubbed out entirely here.
"""
import asyncio
import base64
import io
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.insert(0, ".")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("R2_ACCOUNT_ID", "fake")
os.environ.setdefault("R2_ACCESS_KEY", "fake")
os.environ.setdefault("R2_SECRET_KEY", "fake")
os.environ.setdefault("R2_BUCKET", "fake")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "https://fake.example.com")

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("qa.customer_simulator")

from sqlalchemy.dialects.postgresql import JSONB  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(JSONB, "sqlite")
def _compile_jsonb_sqlite(type_, compiler, **kw):
    return "JSON"


from app import db as db_module  # noqa: E402
import app.models  # noqa: E402
db_module.Base.metadata.create_all(bind=db_module.engine)

from PIL import Image, ImageDraw  # noqa: E402
from app.config import settings  # noqa: E402
from app.anthropic_client import create_message  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app import router  # noqa: E402

# =====================================================================
# Stub out every WhatsApp send / media-download / R2-upload call site,
# following the exact same monkeypatch pattern the test_*.py files use --
# see their headers for precedent. Nothing below touches a real network
# endpoint for WhatsApp or R2; Claude and the image-gen API are the only
# real calls left in the whole pipeline.
# =====================================================================
_captured: list[dict] = []   # this turn's Sakshi replies, in order
_uploaded: dict[str, bytes] = {}  # fake url -> bytes, for every "uploaded" creative/logo/reference


def _make_placeholder_image(kind: str) -> bytes:
    """A simple synthetic image to stand in for whatever the client 'uploads' -- no real photo library needed."""
    img = Image.new("RGB", (800, 800), color=(235, 225, 210) if kind == "product_photo" else (255, 255, 255))
    draw = ImageDraw.Draw(img)
    if kind == "product_photo":
        draw.ellipse((150, 150, 650, 650), fill=(180, 90, 40))
        draw.text((300, 380), "PRODUCT", fill=(255, 255, 255))
    else:  # logo
        draw.rectangle((200, 300, 600, 500), outline=(20, 20, 20), width=12)
        draw.text((320, 380), "LOGO", fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


_PLACEHOLDER_IMAGES = {
    "product_photo": _make_placeholder_image("product_photo"),
    "logo": _make_placeholder_image("logo"),
}


async def _fake_send_text(to, body):
    _captured.append({"speaker": "sakshi", "text": body, "image_bytes": None})


async def _fake_send_image(to, image_url, caption=""):
    _captured.append({"speaker": "sakshi", "text": caption, "image_bytes": _uploaded.get(image_url)})


async def _fake_send_image_with_button(to, image_url, body, button_id, button_label):
    _captured.append({"speaker": "sakshi", "text": body, "image_bytes": _uploaded.get(image_url)})


async def _fake_send_buttons(to, body, buttons):
    labels = ", ".join(f"[{label}]" for _, label in buttons)
    _captured.append({"speaker": "sakshi", "text": f"{body}\n(buttons shown: {labels})", "image_bytes": None})


async def _fake_send_list(to, body, button_text, rows, section_title="Options"):
    labels = ", ".join(title for _, title in rows)
    _captured.append({"speaker": "sakshi", "text": f"{body}\n(list options: {labels})", "image_bytes": None})


async def _fake_download_media(media_id):
    if media_id == "synthetic_product_photo":
        return _PLACEHOLDER_IMAGES["product_photo"]
    if media_id == "synthetic_logo":
        return _PLACEHOLDER_IMAGES["logo"]
    return _PLACEHOLDER_IMAGES["product_photo"]


def _make_fake_uploader(prefix: str):
    def _upload(*args):
        image_bytes = args[-1]
        url = f"https://fake.example.com/{prefix}/{uuid.uuid4()}.png"
        _uploaded[url] = image_bytes
        return url
    return _upload


def _patch_all():
    from app.whatsapp import client as wa_client
    from app import history, instagram, onboarding, payments
    from app.engine import carousel, image_intent, logo_capture, orchestrator

    for mod in (wa_client, router, orchestrator, carousel, image_intent, logo_capture, onboarding, payments, history, instagram):
        if hasattr(mod, "send_text"):
            mod.send_text = _fake_send_text
        if hasattr(mod, "send_image"):
            mod.send_image = _fake_send_image
        if hasattr(mod, "send_image_with_button"):
            mod.send_image_with_button = _fake_send_image_with_button
        if hasattr(mod, "send_buttons"):
            mod.send_buttons = _fake_send_buttons
        if hasattr(mod, "send_list"):
            mod.send_list = _fake_send_list
        if hasattr(mod, "download_media"):
            mod.download_media = _fake_download_media

    fake_upload_creative = _make_fake_uploader("creatives")
    fake_upload_base_image = _make_fake_uploader("creatives")
    fake_upload_carousel_slide = _make_fake_uploader("creatives")
    fake_upload_reference_image = _make_fake_uploader("references")
    fake_upload_logo = _make_fake_uploader("logos")

    orchestrator.upload_creative = fake_upload_creative
    orchestrator.upload_base_image = fake_upload_base_image
    orchestrator.upload_carousel_slide = fake_upload_carousel_slide
    orchestrator.upload_reference_image = fake_upload_reference_image
    image_intent.upload_creative = fake_upload_creative
    image_intent.upload_reference_image = fake_upload_reference_image
    carousel.upload_reference_image = fake_upload_reference_image
    logo_capture.upload_logo = fake_upload_logo


_patch_all()

# =====================================================================
# Personas: each is a name/description (fed to the customer-AI so it
# stays in character) plus a GOAL LIST -- not literal scripted messages.
# The customer-AI phrases each goal as a natural message given the
# persona and how the conversation has gone so far (so wording varies
# run to run, and it can react/adapt to whatever Sakshi actually said,
# rather than blindly following a fixed script). Goals are chosen to
# cover the features that have needed fixing this session: typo
# tolerance, carousels, revisions, reference/reuse ("use the prompt from
# before"), logo upload + placement, and the marketing-consultant
# hard boundary.
# =====================================================================
PERSONAS = [
    {
        "name": "Priya (new bakery owner, moderately tech-savvy, a bit impatient)",
        "phone": "910000000001",
        "description": (
            "Priya just opened a home bakery in Pune. She's testing this bot for the first "
            "time before trusting it with her actual Instagram. She types casually, doesn't "
            "always capitalize, and gets a little short if the bot seems confused."
        ),
        "goals": [
            "Say hi and see what this bot does.",
            "Answer whatever it asks about your business (a home bakery specializing in customized birthday cakes).",
            "Answer whatever it asks about your Instagram page (just say you don't have one yet, or skip).",
            "Answer whatever it asks about brand colors or style (say you like pastel pink and gold, premium feel).",
            "Ask for a weekend offer post: 20% off custom cakes this weekend.",
            "Ask for a revision: make it feel more premium and less cluttered.",
            "Send your bakery's product photo (attach one) and say 'this is my logo, put it somewhere subtle, not covering the cake'.",
            "Ask for another creative -- a post announcing a new chocolate truffle cake -- and see if the logo shows up well placed this time.",
            "Ask a genuine business question: 'how much should I charge for a 6 inch custom cake?'",
        ],
    },
    {
        "name": "Rohan (edge-case tester, deliberately messy typing)",
        "phone": "910000000002",
        "description": (
            "Rohan runs a small electronics repair shop and is specifically testing whether "
            "this bot is robust -- he types with typos on purpose, asks ambiguous things, and "
            "occasionally goes off-topic to see how it reacts."
        ),
        "goals": [
            "Greet the bot with a typo-ridden 'heyyy'.",
            "Describe your business (a phone and laptop repair shop) when asked.",
            "Skip the Instagram question.",
            "Skip or give a brief answer to the brand colors/style question.",
            "Ask for a MISSPELLED carousel -- literally use the word 'carasoul' or 'carsoul' -- for 3 images showing a repair before/after, a customer testimonial, and a discount offer.",
            "A couple of turns later, ask something like 'can you use the prompt from before but make it about laptop screen repairs instead' -- testing whether it remembers and reuses context, not just the literal last image.",
            "Ask something completely off-topic, like what the weather is like today, to see if it redirects you back on-topic.",
            "Ask 'is this a real person or a bot' to check the identity disclosure.",
        ],
    },
]

CUSTOMER_SYSTEM_PROMPT = """You are role-playing as a specific small-business
owner texting a WhatsApp creative-assistant bot called Sakshi. Stay
completely in character -- casual WhatsApp texting style, not formal
writing, matching the persona description given to you.

You'll be given: the persona description, the full conversation so far
(if any), and the SPECIFIC GOAL for your next message. Phrase that goal
as ONE natural message this person would actually send -- don't just
restate the goal verbatim, actually write it the way they'd type it. If
Sakshi's last message asked you a direct question, make sure your message
actually answers it (in character), even if that means adapting the
literal wording of the goal.

If the goal says to send/attach a photo or logo, set "attach_image" to
either "product_photo" or "logo" accordingly; otherwise set it to null.

Reply with JSON only, no other text:
{"message": "...", "attach_image": "product_photo"|"logo"|null}"""


async def _customer_says(persona: dict, transcript: list[dict], goal: str) -> tuple[str, str | None]:
    history_lines = []
    for turn in transcript[-12:]:  # bounded context, this doesn't need the entire history verbatim
        speaker = "You" if turn["speaker"] == "customer" else "Sakshi"
        history_lines.append(f"{speaker}: {turn['text']}")
    history_text = "\n".join(history_lines) if history_lines else "(conversation hasn't started yet)"

    user_content = (
        f"Persona: {persona['description']}\n\n"
        f"Conversation so far:\n{history_text}\n\n"
        f"Your next goal: {goal}"
    )

    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=200,
            system=CUSTOMER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        parsed = json.loads(text)
        return parsed["message"], parsed.get("attach_image")
    except Exception:
        logger.exception("Customer-AI turn failed for goal %r — using the goal text verbatim", goal)
        return goal, None


async def run_scenario(persona: dict) -> list[dict]:
    transcript: list[dict] = []
    phone = persona["phone"]

    for goal in persona["goals"]:
        message, attach_image = await _customer_says(persona, transcript, goal)

        msg = IncomingMessage(
            sender=phone,
            type="image" if attach_image else "text",
            text=message,
            media_id=f"synthetic_{attach_image}" if attach_image else None,
        )
        transcript.append({"speaker": "customer", "text": message, "image_bytes": _PLACEHOLDER_IMAGES.get(attach_image)})

        _captured.clear()
        try:
            await router.handle_message(msg)
        except Exception:
            logger.exception("router.handle_message() raised for persona=%s goal=%r", persona["name"], goal)
            transcript.append({"speaker": "sakshi", "text": "[NO REPLY -- handle_message() raised an exception, see logs]", "image_bytes": None})
            continue
        transcript.extend(_captured)

    return transcript


# =====================================================================
# Expert review: a SEPARATE Claude call, with vision, given the persona
# + full transcript (text and every delivered image). Not the same model
# call as the customer -- deliberately independent, so it isn't just
# grading its own homework.
# =====================================================================
EXPERT_REVIEW_SYSTEM_PROMPT = """You are a creative director and product
QA expert with 25+ years of experience running marketing/creative
agencies and reviewing conversational AI products end-to-end. You've been
given a full WhatsApp conversation transcript between a simulated small-
business customer and "Sakshi," an AI creative assistant, INCLUDING every
image Sakshi actually delivered during the conversation.

Review this transcript and the images with real scrutiny -- the way you'd
review a junior team's work before it ships, not a rubber stamp. Look at:

- Conversational quality: did Sakshi actually understand what the client
  meant, including typos/vague phrasing/references back to earlier in the
  conversation? Did it ask unnecessary questions, repeat itself, or
  ignore something the client said?
- Image quality: is any text or the product cut off / touching the
  edges? Is the product/subject sharp and detailed, or soft/generic? Is
  the composition professional? If a logo was involved, is it placed
  sensibly (not overlapping anything, not just always in one lazy
  corner)?
- Caption/text tone: does delivered caption text read like a real person
  wrote it, or like stiff template copy?
- Correctness: carousels genuinely separate (not one collage image with
  baked-in page numbers)? Revisions actually changing what was asked?
  Off-topic/identity questions handled appropriately?
- Anything that would make a real small-business owner distrust or stop
  using this product.

Write your review as markdown with these sections:
## Summary
(2-3 sentences: overall verdict)

## Issues Found
A markdown table: | Severity (Critical/Major/Minor) | Issue | Where in the conversation | Why it matters |
Sort Critical first. If something is subtle but real, still include it --
don't only report showstoppers. If you genuinely found nothing wrong,
say so plainly rather than inventing issues.

## What's Working Well
Brief, specific -- not generic praise.

Be concrete and specific -- quote the actual message or describe the
actual image defect, don't speak in generalities."""


async def run_expert_review(persona: dict, transcript: list[dict]) -> str:
    content = [{
        "type": "text",
        "text": f"Persona being tested: {persona['name']} — {persona['description']}\n\nFull transcript follows, in order.",
    }]
    for turn in transcript:
        speaker = "CUSTOMER" if turn["speaker"] == "customer" else "SAKSHI"
        content.append({"type": "text", "text": f"[{speaker}]: {turn['text'] or '(image only, no caption)'}"})
        if turn.get("image_bytes"):
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(turn["image_bytes"]).decode("utf-8")},
            })

    try:
        response = await create_message(
            model=settings.CLAUDE_PROMPT_MODEL,
            max_tokens=2000,
            system=EXPERT_REVIEW_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        return response.content[0].text.strip()
    except Exception:
        logger.exception("Expert review failed for persona=%s", persona["name"])
        return "_(Expert review call failed — see the run logs. The transcript below is still real.)_"


def _transcript_to_markdown(transcript: list[dict]) -> str:
    lines = []
    for turn in transcript:
        speaker = "**Customer**" if turn["speaker"] == "customer" else "**Sakshi**"
        img_note = " *(+ image delivered)*" if turn.get("image_bytes") else ""
        lines.append(f"- {speaker}: {turn['text']}{img_note}")
    return "\n".join(lines)


async def _send_telegram_summary(report_lines: list[str]):
    if not settings.ALERT_TELEGRAM_TOKEN or not settings.ALERT_TELEGRAM_CHAT_ID:
        logger.warning("ALERT_TELEGRAM_TOKEN/CHAT_ID not set — skipping Telegram summary")
        return
    import httpx
    text = "\n".join(report_lines)[:4000]  # Telegram's per-message limit
    url = f"https://api.telegram.org/bot{settings.ALERT_TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(url, json={"chat_id": settings.ALERT_TELEGRAM_CHAT_ID, "text": text})
    except Exception:
        logger.exception("Failed to send Telegram summary")


async def main():
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report_sections = [f"# Customer Simulation Report — {run_at}\n"]
    telegram_lines = [f"🧪 Customer simulation complete — {run_at}"]

    for persona in PERSONAS:
        print(f"--- Running scenario: {persona['name']} ---")
        transcript = await run_scenario(persona)
        print(f"--- Reviewing scenario: {persona['name']} ---")
        review = await run_expert_review(persona, transcript)

        report_sections.append(f"## {persona['name']}\n")
        report_sections.append("### Transcript\n")
        report_sections.append(_transcript_to_markdown(transcript))
        report_sections.append("\n### Expert Review\n")
        report_sections.append(review)
        report_sections.append("\n---\n")

        first_line = review.strip().split("\n")[0] if review.strip() else "(no review)"
        telegram_lines.append(f"\n{persona['name']}:\n{first_line}")

    report_path = "qa_report.md"
    with open(report_path, "w") as f:
        f.write("\n".join(report_sections))

    telegram_lines.append(f"\nFull report: see the customer-simulation workflow run artifact ({report_path}).")
    await _send_telegram_summary(telegram_lines)

    print(f"\nReport written to {report_path}")


if __name__ == "__main__":
    asyncio.run(main())
