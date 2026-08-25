"""
Tests for the Meta ad account partner-access schema (app/models.py) and
the WhatsApp connect flow (app/engine/ad_account_connect.py,
app/engine/meta_partner_access.py) -- Phase 2 ads-engine prep work.

Deliberately NOT testing any router.py wiring or campaign-builder
integration -- neither exists in this codebase yet; see
ad_account_connect.py's module docstring. This only verifies:

  - Schema: Business.meta_ad_account_id/meta_business_manager_id/
    partner_access_status and ConversationState.pending_ad_account_connect
    exist with the right defaults.
  - meta_partner_access.normalize_ad_account_id()'s input parsing.
  - meta_partner_access.request_partner_access() /
    check_partner_access_status() against a faked httpx client -- success,
    Graph API error, and missing-config paths.
  - has_ad_account_access() -- only True once partner_access_status is
    actually 'granted'.
  - The full conversational flow end-to-end: has-account yes/no branch,
    ad account ID validation, optional BM ID ("skip"), the partner
    request being sent and partner_access_status set to
    'pending_approval' (never 'granted' on the client's word alone), and
    the confirmation stage only setting 'granted' once
    check_partner_access_status() itself returns CONFIRMED -- a PENDING
    or failed verification must NOT flip the status.
"""
import sys
import asyncio
import os

sys.path.insert(0, ".")
os.environ["DATABASE_URL"] = "sqlite:///./test_ad_account_connect.db"
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

from app.config import settings  # noqa: E402
from app.db import get_session  # noqa: E402
from app.models import Business, ConversationState  # noqa: E402
from app.schemas import IncomingMessage  # noqa: E402
from app.whatsapp import client as wa_client  # noqa: E402

sent_texts, sent_buttons_calls = [], []


async def fake_send_text(to, body):
    sent_texts.append(body)


async def fake_send_buttons(to, body, buttons):
    sent_buttons_calls.append({"body": body, "buttons": buttons})


wa_client.send_text = fake_send_text
wa_client.send_buttons = fake_send_buttons

from app.engine import meta_partner_access  # noqa: E402
meta_partner_access.GRAPH_BASE = "https://graph.fake.example.com/v99.0"

from app.engine import ad_account_connect  # noqa: E402
ad_account_connect.send_text = fake_send_text
ad_account_connect.send_buttons = fake_send_buttons


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", industry="bakery", onboarding_state="done")
        db.add(biz)
        db.flush()
        return biz.id


class _BusinessSnapshot:
    def __init__(self, biz):
        self.meta_ad_account_id = biz.meta_ad_account_id
        self.meta_business_manager_id = biz.meta_business_manager_id
        self.partner_access_status = biz.partner_access_status


def _get_business(biz_id):
    """Returns a detached-safe snapshot (get_session() closes/expires the ORM instance on exit)."""
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        return _BusinessSnapshot(biz)


def _pending(biz_id):
    with get_session() as db:
        convo = db.query(ConversationState).filter(ConversationState.business_id == biz_id).first()
        return convo.pending_ad_account_connect if convo else None


def test_schema_defaults():
    print("=" * 60)
    print("TEST 1: Business partner-access fields exist with the right defaults")
    print("=" * 60)
    biz_id = _make_business("919999999001")
    biz = _get_business(biz_id)
    assert biz.meta_ad_account_id is None
    assert biz.meta_business_manager_id is None
    assert biz.partner_access_status == "not_connected", f"FAIL: expected default 'not_connected', got {biz.partner_access_status!r}"
    print("PASS: meta_ad_account_id/meta_business_manager_id NULL, partner_access_status='not_connected'\n")


def test_normalize_ad_account_id():
    print("=" * 60)
    print("TEST 2: normalize_ad_account_id() parses client input correctly")
    print("=" * 60)
    cases = [
        ("act_123456789", "123456789"),
        ("123456789", "123456789"),
        (" act_987654321 ", "987654321"),
        ("ACT_555", "555"),
        ("not an id", None),
        ("", None),
        ("act_", None),
    ]
    for raw, expected in cases:
        got = meta_partner_access.normalize_ad_account_id(raw)
        assert got == expected, f"FAIL: normalize_ad_account_id({raw!r}) = {got!r}, expected {expected!r}"
    print("PASS: all input variants parsed correctly\n")


def test_has_ad_account_access_gate():
    print("=" * 60)
    print("TEST 3: has_ad_account_access() is True only when partner_access_status == 'granted'")
    print("=" * 60)
    biz_id = _make_business("919999999002")
    biz = _get_business(biz_id)
    assert ad_account_connect.has_ad_account_access(biz) is False

    with get_session() as db:
        b = db.query(Business).filter(Business.id == biz_id).first()
        b.partner_access_status = "pending_approval"
    assert ad_account_connect.has_ad_account_access(_get_business(biz_id)) is False

    with get_session() as db:
        b = db.query(Business).filter(Business.id == biz_id).first()
        b.partner_access_status = "granted"
    assert ad_account_connect.has_ad_account_access(_get_business(biz_id)) is True
    print("PASS: gate correctly tracks not_connected/pending_approval -> False, granted -> True\n")


# --- meta_partner_access.py against a faked httpx client ---

class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text

    def json(self):
        return self._json


class _FakeHttpClient:
    def __init__(self, post_response=None, get_response=None):
        self._post_response = post_response
        self._get_response = get_response
        self.post_calls = []
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None):
        self.post_calls.append({"url": url, "data": data})
        return self._post_response

    async def get(self, url, params=None):
        self.get_calls.append({"url": url, "params": params})
        return self._get_response


def _with_fake_client(fake_client):
    meta_partner_access.httpx.AsyncClient = lambda *a, **kw: fake_client


def _restore_real_client():
    import httpx
    meta_partner_access.httpx.AsyncClient = httpx.AsyncClient


async def run_async():
    print("=" * 60)
    print("TEST 4: request_partner_access() returns False when Meta config is missing")
    print("=" * 60)
    orig_business_id, orig_token = settings.META_BUSINESS_ID, settings.META_SYSTEM_USER_ACCESS_TOKEN
    settings.META_BUSINESS_ID = ""
    settings.META_SYSTEM_USER_ACCESS_TOKEN = ""
    ok = await meta_partner_access.request_partner_access("123456789")
    assert ok is False, "FAIL: expected False with no META_BUSINESS_ID/META_SYSTEM_USER_ACCESS_TOKEN configured"
    settings.META_BUSINESS_ID, settings.META_SYSTEM_USER_ACCESS_TOKEN = orig_business_id, orig_token
    print("PASS: missing config fails safe to False\n")

    settings.META_BUSINESS_ID = "999888777"
    settings.META_SYSTEM_USER_ACCESS_TOKEN = "fake-system-user-token"

    print("=" * 60)
    print("TEST 5: request_partner_access() returns True on a successful Graph API call")
    print("=" * 60)
    fake_client = _FakeHttpClient(post_response=_FakeResponse(200, {"success": True}))
    _with_fake_client(fake_client)
    ok = await meta_partner_access.request_partner_access("123456789")
    assert ok is True, "FAIL: expected True on a 200 response"
    assert len(fake_client.post_calls) == 1
    assert "act_123456789/agencies" in fake_client.post_calls[0]["url"]
    assert fake_client.post_calls[0]["data"]["business"] == "999888777"
    print(f"PASS: {fake_client.post_calls[0]['url']}\n")

    print("=" * 60)
    print("TEST 6: request_partner_access() returns False on a Graph API error response")
    print("=" * 60)
    fake_client = _FakeHttpClient(post_response=_FakeResponse(400, text="Invalid ad account ID"))
    _with_fake_client(fake_client)
    ok = await meta_partner_access.request_partner_access("000000000")
    assert ok is False, "FAIL: expected False on a non-200 response"
    print("PASS: non-200 response correctly treated as failure\n")

    print("=" * 60)
    print("TEST 7: check_partner_access_status() returns CONFIRMED/PENDING correctly")
    print("=" * 60)
    fake_client = _FakeHttpClient(get_response=_FakeResponse(200, {
        "data": [{"id": "999888777", "access_status": "CONFIRMED"}],
    }))
    _with_fake_client(fake_client)
    status = await meta_partner_access.check_partner_access_status("123456789")
    assert status == "CONFIRMED", f"FAIL: expected CONFIRMED, got {status!r}"

    fake_client = _FakeHttpClient(get_response=_FakeResponse(200, {
        "data": [{"id": "999888777", "access_status": "PENDING_CONFIRMATION"}],
    }))
    _with_fake_client(fake_client)
    status = await meta_partner_access.check_partner_access_status("123456789")
    assert status == "PENDING_CONFIRMATION", f"FAIL: expected PENDING_CONFIRMATION, got {status!r}"

    fake_client = _FakeHttpClient(get_response=_FakeResponse(200, {"data": []}))
    _with_fake_client(fake_client)
    status = await meta_partner_access.check_partner_access_status("123456789")
    assert status is None, f"FAIL: expected None when SocioBurp's business isn't in the list, got {status!r}"

    fake_client = _FakeHttpClient(get_response=_FakeResponse(500, text="server error"))
    _with_fake_client(fake_client)
    status = await meta_partner_access.check_partner_access_status("123456789")
    assert status is None, f"FAIL: expected None on a failed Graph API call, got {status!r}"
    print("PASS: CONFIRMED, PENDING, not-found, and error cases all handled correctly\n")

    _restore_real_client()
    settings.META_BUSINESS_ID, settings.META_SYSTEM_USER_ACCESS_TOKEN = orig_business_id, orig_token

    # --- Full conversational flow, mocking meta_partner_access directly ---

    print("=" * 60)
    print("TEST 8: 'No, not yet' branch explains setup and stays not_connected")
    print("=" * 60)
    phone = "919999999010"
    biz_id = _make_business(phone)
    sent_texts.clear()
    sent_buttons_calls.clear()

    await ad_account_connect.start(biz_id, phone)
    assert len(sent_buttons_calls) == 1
    assert _pending(biz_id) is not None

    await ad_account_connect.advance(
        biz_id, IncomingMessage(sender=phone, type="button", button_id=ad_account_connect.BUTTON_HAS_ACCOUNT_NO), _pending(biz_id),
    )
    assert _pending(biz_id) is None, "FAIL: expected the negotiation to end (cleared) on 'not yet'"
    assert _get_business(biz_id).partner_access_status == "not_connected"
    assert "business.facebook.com" in sent_texts[-1]
    print(f"PASS: {sent_texts[-1][:80]}...\n")

    print("=" * 60)
    print("TEST 9: happy path -- yes -> ad account ID -> skip BM ID -> request sent -> pending_approval")
    print("=" * 60)
    phone2 = "919999999011"
    biz_id2 = _make_business(phone2)
    sent_texts.clear()

    request_calls = []

    async def fake_request_partner_access(ad_account_id):
        request_calls.append(ad_account_id)
        return True

    meta_partner_access.request_partner_access = fake_request_partner_access
    ad_account_connect.meta_partner_access.request_partner_access = fake_request_partner_access

    await ad_account_connect.start(biz_id2, phone2)
    await ad_account_connect.advance(
        biz_id2, IncomingMessage(sender=phone2, type="button", button_id=ad_account_connect.BUTTON_HAS_ACCOUNT_YES), _pending(biz_id2),
    )
    assert "Ad Account ID" in sent_texts[-1]

    # invalid ID first -- must NOT advance the stage
    await ad_account_connect.advance(biz_id2, IncomingMessage(sender=phone2, type="text", text="not an id"), _pending(biz_id2))
    assert "valid Ad Account ID" in sent_texts[-1]
    import json as _json
    assert _json.loads(_pending(biz_id2))["stage"] == "awaiting_ad_account_id", "FAIL: an invalid ID must not advance the stage"

    await ad_account_connect.advance(biz_id2, IncomingMessage(sender=phone2, type="text", text="act_555444333"), _pending(biz_id2))
    assert "skip" in sent_texts[-1]
    assert _json.loads(_pending(biz_id2))["stage"] == "awaiting_business_manager_id"

    await ad_account_connect.advance(biz_id2, IncomingMessage(sender=phone2, type="text", text="skip"), _pending(biz_id2))
    assert request_calls == ["555444333"], f"FAIL: expected the normalized ad account ID sent to the Marketing API, got {request_calls}"
    biz2 = _get_business(biz_id2)
    assert biz2.meta_ad_account_id == "555444333"
    assert biz2.meta_business_manager_id is None
    assert biz2.partner_access_status == "pending_approval", f"FAIL: expected 'pending_approval' after the request was sent, got {biz2.partner_access_status!r}"
    assert "Business Settings" in sent_texts[-1] and "Partners" in sent_texts[-1]
    assert _json.loads(_pending(biz_id2))["stage"] == "awaiting_approval_confirmation"
    print("PASS: partner request sent, status set to pending_approval (not granted), told exactly where to approve\n")

    print("=" * 60)
    print("TEST 10: confirmation stage -- a still-PENDING Meta response must NOT flip status to granted")
    print("=" * 60)
    sent_texts.clear()

    async def fake_check_pending(ad_account_id):
        return "PENDING"

    meta_partner_access.check_partner_access_status = fake_check_pending
    ad_account_connect.meta_partner_access.check_partner_access_status = fake_check_pending

    await ad_account_connect.advance(biz_id2, IncomingMessage(sender=phone2, type="text", text="I approved it"), _pending(biz_id2))
    biz2 = _get_business(biz_id2)
    assert biz2.partner_access_status == "pending_approval", f"FAIL: a PENDING verification must not set 'granted', got {biz2.partner_access_status!r}"
    assert _pending(biz_id2) is not None, "FAIL: the negotiation must stay open until actually confirmed"
    assert "Business Settings" in sent_texts[-1]
    print("PASS: client's own 'I approved it' claim alone did not flip status -- still pending_approval\n")

    print("=" * 60)
    print("TEST 11: confirmation stage -- a CONFIRMED Meta response sets status to granted and clears the negotiation")
    print("=" * 60)
    sent_texts.clear()

    async def fake_check_confirmed(ad_account_id):
        return "CONFIRMED"

    meta_partner_access.check_partner_access_status = fake_check_confirmed
    ad_account_connect.meta_partner_access.check_partner_access_status = fake_check_confirmed

    await ad_account_connect.advance(biz_id2, IncomingMessage(sender=phone2, type="text", text="done"), _pending(biz_id2))
    biz2 = _get_business(biz_id2)
    assert biz2.partner_access_status == "granted", f"FAIL: expected 'granted' after a CONFIRMED verification, got {biz2.partner_access_status!r}"
    assert _pending(biz_id2) is None, "FAIL: expected the negotiation cleared once granted"
    print(f"PASS: {sent_texts[-1]}\n")

    print("=" * 60)
    print("TEST 12: a failed partner-access request does not fabricate 'pending_approval'")
    print("=" * 60)
    phone3 = "919999999012"
    biz_id3 = _make_business(phone3)
    sent_texts.clear()

    async def fake_request_fails(ad_account_id):
        return False

    meta_partner_access.request_partner_access = fake_request_fails
    ad_account_connect.meta_partner_access.request_partner_access = fake_request_fails

    await ad_account_connect.start(biz_id3, phone3)
    await ad_account_connect.advance(
        biz_id3, IncomingMessage(sender=phone3, type="button", button_id=ad_account_connect.BUTTON_HAS_ACCOUNT_YES), _pending(biz_id3),
    )
    await ad_account_connect.advance(biz_id3, IncomingMessage(sender=phone3, type="text", text="act_111222333"), _pending(biz_id3))
    await ad_account_connect.advance(biz_id3, IncomingMessage(sender=phone3, type="text", text="skip"), _pending(biz_id3))

    biz3 = _get_business(biz_id3)
    assert biz3.partner_access_status == "not_connected", f"FAIL: a failed API call must not set pending_approval, got {biz3.partner_access_status!r}"
    assert _pending(biz_id3) is None, "FAIL: expected the negotiation cleared after the failure"
    print("PASS: failed request left partner_access_status untouched, negotiation ended cleanly\n")

    print("ALL TESTS PASSED")


def run():
    test_schema_defaults()
    test_normalize_ad_account_id()
    test_has_ad_account_access_gate()
    asyncio.run(run_async())


run()
