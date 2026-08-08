"""
Test for app/whatsapp/webhook.py's parse_message() — specifically the fix
for image messages silently dropping their caption.

Previously, IncomingMessage for an image message never read
msg["image"].get("caption"), so a client attaching text to a photo (the
natural way to say "edit this: change the background to black") ended up
with text=None. That immediately tripped orchestrator.generate()'s
`if not msg.text: ask them to describe as text` guard — the instruction
was silently lost and the client was asked to repeat themselves, even
though they'd already said it.

Covers:
  - image message WITH a caption -> text is the caption
  - image message with NO caption -> text is None (unchanged, correct)
  - text and button-reply messages still parse correctly (no regression)
  - an unrecognized message type still returns None
"""
import sys
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_webhook_parse.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

from app.whatsapp.webhook import parse_message  # noqa: E402


def _payload(message: dict) -> dict:
    return {
        "entry": [{
            "changes": [{
                "value": {"messages": [message]},
            }],
        }],
    }


def run():
    print("=" * 60)
    print("TEST 1: image message WITH a caption -> text is the caption")
    print("=" * 60)
    msg = parse_message(_payload({
        "from": "919999999920", "id": "wamid.IMG1", "type": "image",
        "image": {"id": "media123", "caption": "change the background to black, add 25% off overlay"},
    }))
    assert msg is not None, "FAIL: expected a parsed message"
    assert msg.type == "image"
    assert msg.media_id == "media123"
    assert msg.text == "change the background to black, add 25% off overlay", (
        f"FAIL: expected the caption captured as text, got {msg.text!r}"
    )
    print(f"PASS: caption captured: {msg.text!r}\n")

    print("=" * 60)
    print("TEST 2: image message with NO caption -> text is None")
    print("=" * 60)
    msg = parse_message(_payload({
        "from": "919999999920", "id": "wamid.IMG2", "type": "image",
        "image": {"id": "media456"},
    }))
    assert msg is not None
    assert msg.text is None, f"FAIL: expected no text for a captionless image, got {msg.text!r}"
    print("PASS: captionless image correctly has text=None\n")

    print("=" * 60)
    print("TEST 3: text message still parses correctly")
    print("=" * 60)
    msg = parse_message(_payload({
        "from": "919999999920", "id": "wamid.TXT1", "type": "text",
        "text": {"body": "Create a weekend offer post"},
    }))
    assert msg is not None
    assert msg.type == "text"
    assert msg.text == "Create a weekend offer post"
    print("PASS: text message unaffected\n")

    print("=" * 60)
    print("TEST 4: button reply still parses correctly")
    print("=" * 60)
    msg = parse_message(_payload({
        "from": "919999999920", "id": "wamid.BTN1", "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "post_ig_abc", "title": "Post to Instagram"}},
    }))
    assert msg is not None
    assert msg.type == "button"
    assert msg.button_id == "post_ig_abc"
    print("PASS: button reply unaffected\n")

    print("=" * 60)
    print("TEST 5: unrecognized message type -> None")
    print("=" * 60)
    msg = parse_message(_payload({"from": "919999999920", "id": "wamid.X", "type": "sticker"}))
    assert msg is None, f"FAIL: expected None for an unhandled type, got {msg}"
    print("PASS: unrecognized type correctly ignored\n")

    print("ALL TESTS PASSED")


run()
