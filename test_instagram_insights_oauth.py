"""
Tests for app/instagram_insights_oauth.py (per-business Facebook Login for
Business -> Instagram Insights OAuth) and app/engine/instagram_insights.py
(the read client that uses the resulting token).

Covers: signed-link/state round-trip (valid, tampered, expired, malformed),
send_connect_link()'s not-configured guard, /oauth/instagram/start's token
verification and redirect construction, /oauth/instagram/callback's full
happy path (token exchange -> IG business account lookup -> stored on
Business -> WhatsApp confirmation), the user-denied path, the "approved but
no linked IG Business account" path, a token-exchange failure, and the
Insights read client's not-connected / success / Graph-API-error /
exception paths.

All Claude/DB/WhatsApp/Graph-API calls mocked or run against sqlite --
this is a control-flow test, no real network calls.
"""
import sys
import asyncio
import os
import time
import logging
import uuid

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_instagram_insights_oauth.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("R2_ACCOUNT_ID", "fake")
os.environ.setdefault("R2_ACCESS_KEY", "fake")
os.environ.setdefault("R2_SECRET_KEY", "fake")
os.environ.setdefault("R2_BUCKET", "fake")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "https://fake.example.com")
os.environ.setdefault("IMAGE_API_KEY", "fake")
os.environ.setdefault("META_APP_ID", "fake-app-id")
os.environ.setdefault("META_APP_SECRET", "fake-app-secret")
os.environ.setdefault("META_OAUTH_REDIRECT_URI", "https://socioburp.example.com/oauth/instagram/callback")

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
from app.models import Business  # noqa: E402
from app import instagram_insights_oauth as oauth  # noqa: E402
from app.engine import instagram_insights as insights  # noqa: E402

sent = []


async def fake_send_text(to, body):
    sent.append(body)


oauth.send_text = fake_send_text

log_records = []


class _ListHandler(logging.Handler):
    def emit(self, record):
        log_records.append((record.levelname, record.getMessage()))


oauth.logger.addHandler(_ListHandler())
oauth.logger.setLevel(logging.INFO)


class _FakeResponse:
    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or str(json_data)

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _FakeQueryParams(dict):
    pass


class _FakeRequest:
    def __init__(self, **params):
        self.query_params = _FakeQueryParams(params)


def _make_business(phone):
    with get_session() as db:
        biz = Business(phone=phone, name="Test Biz", onboarding_state="done")
        db.add(biz)
        db.flush()
        return biz.id


async def run():
    print("=" * 60)
    print("TEST 1: signed token round-trip -- valid, tampered, expired, malformed")
    print("=" * 60)
    biz_id = _make_business("919999999950")
    token = oauth._make_token(biz_id)
    assert oauth._verify_token(token) == biz_id, "FAIL: a freshly-made token should verify back to its business_id"
    print("PASS: fresh token verifies correctly")

    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert oauth._verify_token(tampered) is None, "FAIL: a tampered signature must be rejected"
    print("PASS: tampered signature rejected")

    biz_id_str, ts_str, sig = token.split(".", 2)
    old_ts = int(ts_str) - oauth.LINK_TTL_SECONDS - 1
    old_sig = oauth._sign(biz_id, old_ts)
    expired_token = f"{biz_id_str}.{old_ts}.{old_sig}"
    assert oauth._verify_token(expired_token) is None, "FAIL: an expired (correctly-signed) token must be rejected"
    print("PASS: expired token rejected even with a correct signature")

    assert oauth._verify_token("not-a-real-token") is None, "FAIL: a malformed token must be rejected, not raise"
    assert oauth._verify_token("") is None, "FAIL: an empty token must be rejected, not raise"
    print("PASS: malformed/empty tokens rejected without raising\n")

    print("=" * 60)
    print("TEST 2: send_connect_link -- not configured vs. configured")
    print("=" * 60)
    sent.clear()
    real_app_id = settings.META_APP_ID
    settings.META_APP_ID = ""
    await oauth.send_connect_link(biz_id, "919999999950")
    assert len(sent) == 1 and "isn't set up" in sent[0].lower(), f"FAIL: expected not-configured message, got {sent}"
    print(f"PASS: not-configured guard fires: {sent[0]!r}")
    settings.META_APP_ID = real_app_id

    sent.clear()
    await oauth.send_connect_link(biz_id, "919999999950")
    assert len(sent) == 1, f"FAIL: expected exactly one message, got {sent}"
    assert "/oauth/instagram/start?token=" in sent[0], f"FAIL: expected a connect link, got {sent[0]!r}"
    assert str(biz_id) in [
        oauth._verify_token(sent[0].split("token=")[1].split()[0]) and str(biz_id)
    ], "sanity"
    sent_token = sent[0].split("token=")[1].split()[0].strip()
    assert oauth._verify_token(sent_token) == biz_id, "FAIL: the token embedded in the WhatsApp link must verify to this business"
    print(f"PASS: connect link sent with a valid, verifiable token\n")

    print("=" * 60)
    print("TEST 3: /oauth/instagram/start -- invalid token vs. valid token redirect")
    print("=" * 60)
    resp = await oauth.oauth_start(token="garbage")
    assert resp.status_code == 400, f"FAIL: expected 400 for an invalid token, got {resp.status_code}"
    print("PASS: invalid token -> 400")

    resp = await oauth.oauth_start(token=sent_token)
    assert resp.status_code in (302, 307), f"FAIL: expected a redirect, got {resp.status_code}"
    location = resp.headers["location"]
    assert location.startswith(oauth.AUTH_DIALOG_BASE), f"FAIL: expected redirect to Meta's auth dialog, got {location}"
    assert f"state={sent_token}" in location, f"FAIL: expected state param to carry the same signed token, got {location}"
    assert "instagram_manage_insights" in location, f"FAIL: expected the insights scope in the auth URL, got {location}"
    print(f"PASS: valid token -> redirect to Meta with matching state: {location}\n")

    print("=" * 60)
    print("TEST 4: /oauth/instagram/callback -- user denied")
    print("=" * 60)
    sent.clear()
    resp = await oauth.oauth_callback(_FakeRequest(state=sent_token, error="access_denied", error_description="user cancelled"))
    assert len(sent) == 1 and "cancelled" in sent[0].lower(), f"FAIL: expected a cancellation message, got {sent}"
    assert resp.status_code == 200, f"FAIL: cancellation should still render a plain HTML page, got {resp.status_code}"
    print(f"PASS: {sent[0]!r}\n")

    print("=" * 60)
    print("TEST 5: /oauth/instagram/callback -- invalid/expired state, no code")
    print("=" * 60)
    sent.clear()
    resp = await oauth.oauth_callback(_FakeRequest(state="garbage", code="abc"))
    assert resp.status_code == 400, f"FAIL: expected 400 for invalid state, got {resp.status_code}"
    assert len(sent) == 0, f"FAIL: should not message anyone if we can't identify the business, got {sent}"
    print("PASS: invalid state -> 400, no message sent (we don't know who to tell)\n")

    print("=" * 60)
    print("TEST 6: /oauth/instagram/callback -- full happy path")
    print("=" * 60)
    sent.clear()
    fresh_token = oauth._make_token(biz_id)

    call_log = []

    async def fake_get(self, url, params=None, **kwargs):
        call_log.append(url)
        if url.endswith("/oauth/access_token") and params.get("code"):
            return _FakeResponse(200, {"access_token": "short-lived-token", "expires_in": 3600})
        if url.endswith("/oauth/access_token") and params.get("grant_type") == "fb_exchange_token":
            return _FakeResponse(200, {"access_token": "long-lived-user-token", "expires_in": 5184000})
        if url.endswith("/me/accounts"):
            return _FakeResponse(200, {
                "data": [
                    {"id": "page-1", "name": "No IG here", "access_token": "page-1-token"},
                    {
                        "id": "page-2", "name": "Has IG", "access_token": "page-2-token",
                        "instagram_business_account": {"id": "ig-user-42", "username": "testbiz"},
                    },
                ]
            })
        raise AssertionError(f"unexpected GET to {url}")

    import httpx
    real_get = httpx.AsyncClient.get
    httpx.AsyncClient.get = fake_get

    resp = await oauth.oauth_callback(_FakeRequest(state=fresh_token, code="auth-code-123"))

    httpx.AsyncClient.get = real_get

    assert resp.status_code == 200, f"FAIL: expected a 200 success page, got {resp.status_code}"
    assert len(sent) == 1 and "connected" in sent[0].lower() and "@testbiz" in sent[0], f"FAIL: expected a success message with the username, got {sent}"
    print(f"PASS: {sent[0]!r}")

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id).first()
        assert biz.instagram_insights_ig_user_id == "ig-user-42", f"FAIL: expected the SECOND page's IG account (first has none), got {biz.instagram_insights_ig_user_id}"
        assert biz.instagram_insights_page_id == "page-2"
        assert biz.instagram_insights_access_token == "page-2-token"
        assert biz.instagram_insights_connected_at is not None
        assert biz.instagram_insights_token_expires_at is not None
    print("PASS: correct page (the one WITH a linked IG account) stored, not the first page in the list\n")

    print("=" * 60)
    print("TEST 7: /oauth/instagram/callback -- approved but no linked IG Business account anywhere")
    print("=" * 60)
    biz_id7 = _make_business("919999999951")
    token7 = oauth._make_token(biz_id7)
    sent.clear()

    async def fake_get_no_ig(self, url, params=None, **kwargs):
        if url.endswith("/oauth/access_token"):
            return _FakeResponse(200, {"access_token": "tok", "expires_in": 3600})
        if url.endswith("/me/accounts"):
            return _FakeResponse(200, {"data": [{"id": "page-x", "name": "No IG", "access_token": "px"}]})
        raise AssertionError(f"unexpected GET to {url}")

    httpx.AsyncClient.get = fake_get_no_ig
    resp = await oauth.oauth_callback(_FakeRequest(state=token7, code="auth-code-456"))
    httpx.AsyncClient.get = real_get

    assert resp.status_code == 200
    assert len(sent) == 1 and "couldn't find" in sent[0].lower(), f"FAIL: expected the no-linked-account message, got {sent}"
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id7).first()
        assert biz.instagram_insights_ig_user_id is None, "FAIL: must not store a connection when none was found"
    print(f"PASS: {sent[0]!r}\n")

    print("=" * 60)
    print("TEST 8: /oauth/instagram/callback -- token exchange raises -> failure message, 502, no crash")
    print("=" * 60)
    biz_id8 = _make_business("919999999952")
    token8 = oauth._make_token(biz_id8)
    sent.clear()
    log_records.clear()

    async def fake_get_raises(self, url, params=None, **kwargs):
        raise httpx.ConnectTimeout("boom")

    httpx.AsyncClient.get = fake_get_raises
    resp = await oauth.oauth_callback(_FakeRequest(state=token8, code="auth-code-789"))
    httpx.AsyncClient.get = real_get

    assert resp.status_code == 502, f"FAIL: expected 502 on a token-exchange failure, got {resp.status_code}"
    assert len(sent) == 1 and "failed" in sent[0].lower(), f"FAIL: expected a failure message, got {sent}"
    error_logs = [msg for level, msg in log_records if level == "ERROR"]
    assert len(error_logs) >= 1, f"FAIL: expected the failure logged, got {log_records}"
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id8).first()
        assert biz.instagram_insights_ig_user_id is None
    print(f"PASS: {sent[0]!r}, logged, no data stored\n")

    print("=" * 60)
    print("TEST 9: Insights read client -- not connected, connected+success, Graph error, exception")
    print("=" * 60)
    biz_id9 = _make_business("919999999953")

    result = await insights.get_account_insights(biz_id9)
    assert result is None, f"FAIL: expected None for a business that never connected, got {result}"
    assert insights.is_connected(biz_id9) is False
    print("PASS: not-connected business returns None, is_connected() is False")

    with get_session() as db:
        biz = db.query(Business).filter(Business.id == biz_id9).first()
        biz.instagram_insights_ig_user_id = "ig-user-99"
        biz.instagram_insights_access_token = "tok-99"

    assert insights.is_connected(biz_id9) is True

    async def fake_get_insights_ok(self, url, params=None, **kwargs):
        assert "ig-user-99" in url, f"FAIL: expected the stored ig_user_id in the URL, got {url}"
        assert params["access_token"] == "tok-99", f"FAIL: expected the stored token used, got {params}"
        return _FakeResponse(200, {"data": [{"name": "reach", "values": [{"value": 42}]}]})

    httpx.AsyncClient.get = fake_get_insights_ok
    result = await insights.get_account_insights(biz_id9)
    httpx.AsyncClient.get = real_get
    assert result == {"data": [{"name": "reach", "values": [{"value": 42}]}]}, f"FAIL: expected the parsed Graph response, got {result}"
    print(f"PASS: connected business gets real Insights data: {result}")

    async def fake_get_insights_error(self, url, params=None, **kwargs):
        return _FakeResponse(400, {"error": "bad token"}, text="Invalid OAuth access token")

    httpx.AsyncClient.get = fake_get_insights_error
    result = await insights.get_account_insights(biz_id9)
    httpx.AsyncClient.get = real_get
    assert result is None, f"FAIL: a Graph API error should degrade to None, not raise or return the error body, got {result}"
    print("PASS: Graph API error (e.g. expired token) degrades to None")

    async def fake_get_insights_raises(self, url, params=None, **kwargs):
        raise httpx.ConnectTimeout("boom")

    httpx.AsyncClient.get = fake_get_insights_raises
    result = await insights.get_media_insights(biz_id9, "media-123")
    httpx.AsyncClient.get = real_get
    assert result is None, f"FAIL: a network exception should degrade to None, not raise, got {result}"
    print("PASS: network exception on media insights degrades to None, doesn't raise\n")

    print("ALL TESTS PASSED")


asyncio.run(run())
