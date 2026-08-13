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
  - a list reply (send_list()'s tap-to-open menu, e.g. the carousel
    slide-count picker) parses with the same shape as a button reply
  - an unrecognized message type (voice note, video, sticker, etc.)
    parses as type="unsupported" -- NOT None, which used to mean total
    silence, never a reply to the client (see app/router.py's handling)
  - a genuinely non-message webhook event (status update) still None
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
    print("TEST 4b: list reply (e.g. carousel slide-count picker) parses like a button reply")
    print("=" * 60)
    msg = parse_message(_payload({
        "from": "919999999920", "id": "wamid.LIST1", "type": "interactive",
        "interactive": {"type": "list_reply", "list_reply": {"id": "carousel_count_5", "title": "5 images"}},
    }))
    assert msg is not None
    assert msg.type == "button", f"FAIL: expected list replies handled like button replies, got type={msg.type!r}"
    assert msg.button_id == "carousel_count_5", f"FAIL: expected the row id as button_id, got {msg.button_id!r}"
    assert msg.text == "5 images", f"FAIL: expected the row title as text, got {msg.text!r}"
    print("PASS: list reply parsed with the same shape as a button reply\n")

    print("=" * 60)
    print("TEST 5: unrecognized message type (voice note, video, sticker, ...) -> type='unsupported', NOT None")
    print("=" * 60)
    # Previously this returned None, and the client got total silence --
    # the same failure mode as the "uploaded image with no caption" bug.
    # type="unsupported" (carrying sender/message_id through, for dedup and
    # so app/router.py can reply) lets router.py send an honest "can't
    # handle that yet" message instead. See app/router.py's
    # `if msg.type == "unsupported":` check.
    for unhandled_type in ("sticker", "audio", "video", "document", "location", "contacts", "reaction", "order"):
        msg = parse_message(_payload({"from": "919999999920", "id": f"wamid.{unhandled_type}", "type": unhandled_type}))
        assert msg is not None, f"FAIL: expected an IncomingMessage for type={unhandled_type!r}, got None (silent drop)"
        assert msg.type == "unsupported", f"FAIL: expected type='unsupported' for {unhandled_type!r}, got {msg.type!r}"
        assert msg.sender == "919999999920"
        assert msg.message_id == f"wamid.{unhandled_type}", "FAIL: expected message_id preserved for dedup"
    print("PASS: every unhandled message type parses as type='unsupported', never silently dropped\n")

    print("=" * 60)
    print("TEST 6: a webhook event with no 'messages' key (e.g. a delivery/read status update) -> still None")
    print("=" * 60)
    status_payload = {"entry": [{"changes": [{"value": {"statuses": [{"status": "delivered"}]}}]}]}
    msg = parse_message(status_payload)
    assert msg is None, f"FAIL: expected None for a non-message webhook event, got {msg}"
    print("PASS: non-message events (status updates) still correctly ignored\n")

    print("ALL TESTS PASSED")


run()
