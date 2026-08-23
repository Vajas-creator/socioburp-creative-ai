"""
Test for the Aug 2026 "bot silent after 'give me a moment'" root-cause
fix in app/storage.py.

Investigation: reported fixed once already (routing brand_reflection.
reflect_first_result() through orchestrator._run_generation()'s try/
except, so a fast-RAISING failure there gets caught and reported), but
confirmed still broken on a live number. Traced every network call that
runs between "give me a moment" and the client's next reply -- prompt
build, image generation, quality check, text/logo compositing, caption,
and the R2 upload -- and found the R2 upload was the only one with no
explicit timeout: app/storage.py's boto3 client was constructed with only
`Config(signature_version="s3v4")`, no connect_timeout/read_timeout/
retries, meaning it ran on botocore's own defaults (60s connect + 60s
read, PER RETRY ATTEMPT). A transient R2 network hiccup could silently
block a generation for several minutes with ZERO log output and ZERO
message to the client, because Python can't reach any except block while
still awaiting inside the try -- a fast-raising exception was never the
actual failure mode here, an unbounded hang was.

Fix: explicit short connect/read timeouts + a small bounded retry count
on the R2 client, plus an explicit, specifically-labeled log line around
the actual put_object call, so a real R2 failure is now (a) fast --
~15-20s instead of minutes, which lets it flow into
orchestrator._run_generation()'s EXISTING exception handling (alert the
team + "Something went wrong" to the client) instead of hanging past the
point where the user gives up waiting, and (b) immediately identifiable
in logs as an R2 upload failure specifically, not a generic unlabeled
crash somewhere in the pipeline.

Covers:
  - _get_client() configures the expected short connect/read timeouts and
    a bounded retry count -- this is the actual root-cause fix; a
    regression here silently reintroduces the multi-minute hang.
  - _upload() logs a specifically-labeled exception and re-raises (not
    swallows) on a put_object failure, so the caller's existing handling
    still runs.
  - _upload() still succeeds and logs normally when put_object succeeds
    -- unchanged behavior for the working case.
  - upload_logo/upload_creative/upload_base_image/upload_carousel_slide/
    upload_reference_image all route through the same fixed _upload(),
    so the fix applies uniformly everywhere R2 is touched.
"""
import sys
import logging
import os

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_storage_timeout.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")
os.environ.setdefault("R2_ACCOUNT_ID", "fake-account")
os.environ.setdefault("R2_ACCESS_KEY", "fake-key")
os.environ.setdefault("R2_SECRET_KEY", "fake-secret")
os.environ.setdefault("R2_BUCKET", "fake-bucket")
os.environ.setdefault("R2_PUBLIC_BASE_URL", "https://cdn.fake.example.com")

import uuid  # noqa: E402
from app import storage  # noqa: E402


def test_client_has_bounded_timeouts_and_retries():
    print("=" * 60)
    print("TEST 1: R2 client is configured with short, explicit connect/read timeouts")
    print("=" * 60)
    storage._client = None  # force a fresh client so this test isn't order-dependent
    client = storage._get_client()
    cfg = client.meta.config

    assert cfg.connect_timeout is not None and cfg.connect_timeout <= 15, (
        f"FAIL: expected a short explicit connect_timeout (<=15s), got {cfg.connect_timeout!r} "
        "-- an unset/default timeout is exactly the root cause of the silent-hang bug"
    )
    assert cfg.read_timeout is not None and cfg.read_timeout <= 30, (
        f"FAIL: expected a short explicit read_timeout (<=30s), got {cfg.read_timeout!r}"
    )
    print(f"PASS: connect_timeout={cfg.connect_timeout}s, read_timeout={cfg.read_timeout}s\n")

    print("=" * 60)
    print("TEST 2: R2 client has a bounded (not unlimited) retry count configured")
    print("=" * 60)
    retries = cfg.retries
    assert retries is not None, "FAIL: expected explicit retry configuration, got None (botocore defaults apply)"
    total_attempts = retries.get("total_max_attempts") or retries.get("max_attempts")
    assert total_attempts is not None and total_attempts <= 5, (
        f"FAIL: expected a small, bounded total attempt count, got {retries!r}"
    )
    print(f"PASS: retries={retries}\n")

    print("=" * 60)
    print("TEST 3: _get_client() reuses the same client instance (unchanged caching behavior)")
    print("=" * 60)
    client2 = storage._get_client()
    assert client2 is client, "FAIL: expected the module-level client to be cached/reused, not rebuilt every call"
    print("PASS: client instance reused\n")
    storage._client = None  # reset for later tests


def test_upload_logs_and_reraises_on_failure(caplog=None):
    print("=" * 60)
    print("TEST 4: _upload() logs a specifically-labeled exception and re-raises on failure")
    print("=" * 60)

    class _FailingClient:
        def put_object(self, **kwargs):
            raise ConnectionError("simulated R2 network failure")

    storage._client = _FailingClient()

    logged = []

    class _CapturingHandler(logging.Handler):
        def emit(self, record):
            logged.append(record.getMessage())

    handler = _CapturingHandler()
    storage.logger.addHandler(handler)
    storage.logger.setLevel(logging.DEBUG)

    raised = None
    try:
        storage._upload("test/key.png", b"fake-bytes", "image/png")
    except ConnectionError as exc:
        raised = exc
    finally:
        storage.logger.removeHandler(handler)

    assert raised is not None, "FAIL: _upload() must re-raise on failure, not swallow it -- the caller's existing exception handling depends on this"
    assert any("R2 upload failed" in msg and "test/key.png" in msg for msg in logged), (
        f"FAIL: expected a specifically-labeled 'R2 upload failed' log line naming the key, got {logged}"
    )
    print(f"PASS: re-raised ConnectionError, logged: {[m for m in logged if 'R2 upload failed' in m]}\n")

    storage._client = None


def test_upload_succeeds_normally():
    print("=" * 60)
    print("TEST 5: _upload() still succeeds and returns the expected URL on a working call")
    print("=" * 60)

    class _WorkingClient:
        def __init__(self):
            self.calls = []

        def put_object(self, **kwargs):
            self.calls.append(kwargs)

    fake_client = _WorkingClient()
    storage._client = fake_client

    url = storage._upload("creatives/abc/xyz.png", b"fake-image-bytes", "image/png")

    assert url == "https://cdn.fake.example.com/creatives/abc/xyz.png", f"FAIL: unexpected URL {url!r}"
    assert len(fake_client.calls) == 1
    assert fake_client.calls[0]["Bucket"] == "fake-bucket"
    assert fake_client.calls[0]["Key"] == "creatives/abc/xyz.png"
    assert fake_client.calls[0]["ContentType"] == "image/png"
    print(f"PASS: {url}\n")

    storage._client = None


def test_all_upload_functions_route_through_fixed_upload():
    print("=" * 60)
    print("TEST 6: every upload_* helper routes through the same (fixed) _upload()")
    print("=" * 60)

    class _WorkingClient:
        def __init__(self):
            self.keys = []

        def put_object(self, **kwargs):
            self.keys.append(kwargs["Key"])

    fake_client = _WorkingClient()
    storage._client = fake_client

    biz_id = uuid.uuid4()
    gen_id = uuid.uuid4()

    storage.upload_logo(biz_id, b"logo-bytes")
    storage.upload_creative(biz_id, gen_id, b"creative-bytes")
    storage.upload_base_image(biz_id, gen_id, b"base-bytes")
    storage.upload_carousel_slide(biz_id, gen_id, 2, b"slide-bytes")
    storage.upload_reference_image(biz_id, b"ref-bytes")

    assert len(fake_client.keys) == 5, f"FAIL: expected 5 uploads to have gone through _upload(), got {fake_client.keys}"
    assert fake_client.keys[0] == f"logos/{biz_id}.png"
    assert fake_client.keys[1] == f"creatives/{biz_id}/{gen_id}.png"
    assert fake_client.keys[2] == f"creatives/{biz_id}/{gen_id}_base.png"
    assert fake_client.keys[3] == f"creatives/{biz_id}/{gen_id}_slide2.png"
    assert fake_client.keys[4].startswith(f"references/{biz_id}/")
    print(f"PASS: all 5 upload helpers produced the expected keys: {fake_client.keys}\n")

    storage._client = None


def run():
    test_client_has_bounded_timeouts_and_retries()
    test_upload_logs_and_reraises_on_failure()
    test_upload_succeeds_normally()
    test_all_upload_functions_route_through_fixed_upload()
    print("ALL TESTS PASSED")


run()
