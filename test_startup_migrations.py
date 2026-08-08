"""
Test for app/db.py's run_migrations() -- runs `alembic upgrade head`
programmatically at application startup (app/main.py's startup event),
since Render's free plan has no Shell or Pre-Deploy Command to run
migrations out-of-band before a deploy goes live.

Real migrations in this repo are Postgres-only from the very first one
(migration 0001 runs `CREATE EXTENSION IF NOT EXISTS "pgcrypto"`), same
limitation every other test in this repo works around by using
Base.metadata.create_all() against SQLite instead of running Alembic for
real -- there's no Postgres available in this environment to actually
exercise the full chain end-to-end here either. What IS testable, and is
exactly the bug this fixes: Alembic resolves alembic.ini's relative
`script_location = migrations` against the process's current working
directory, NOT against alembic.ini's own location -- so a naive
Config("path/to/alembic.ini") silently breaks the moment the app is
started from any directory other than the repo root. This test proves
run_migrations() builds its Config with script_location forced to an
absolute path, and that this holds regardless of the process's CWD.
(The full chain against a real Postgres was exercised manually and is
documented as working in the PR — see the conversation this shipped in.)
"""
import os
import sys

sys.path.insert(0, ".")
os.environ.setdefault("DATABASE_URL", "sqlite:///./test_startup_migrations.db")
os.environ.setdefault("WA_VERIFY_TOKEN", "fake")
os.environ.setdefault("WA_ACCESS_TOKEN", "fake")
os.environ.setdefault("WA_PHONE_NUMBER_ID", "fake")
os.environ.setdefault("ANTHROPIC_API_KEY", "fake")

import alembic.command  # noqa: E402
from app import db as db_module  # noqa: E402

calls = []


def fake_upgrade(config, revision):
    calls.append({
        "revision": revision,
        "script_location": config.get_main_option("script_location"),
    })


def run():
    print("=" * 60)
    print("TEST 1: run_migrations() targets head with an absolute script_location")
    print("=" * 60)
    calls.clear()
    alembic.command.upgrade = fake_upgrade

    original_cwd = os.getcwd()
    db_module.run_migrations()

    assert os.getcwd() == original_cwd, "FAIL: run_migrations() should not change the process CWD"
    assert len(calls) == 1, f"FAIL: expected exactly one upgrade() call, got {calls}"
    assert calls[0]["revision"] == "head", f"FAIL: expected 'head', got {calls[0]['revision']!r}"

    script_location = calls[0]["script_location"]
    assert os.path.isabs(script_location), f"FAIL: expected an absolute script_location, got {script_location!r}"
    assert script_location.endswith("migrations"), f"FAIL: expected it to point at the migrations dir, got {script_location!r}"
    assert os.path.isdir(script_location), f"FAIL: resolved script_location doesn't actually exist: {script_location!r}"
    print(f"PASS: script_location correctly resolved to an absolute path: {script_location!r}\n")

    print("=" * 60)
    print("TEST 2: same result regardless of the process's current working directory")
    print("=" * 60)
    calls.clear()
    os.chdir("/tmp")
    try:
        db_module.run_migrations()
    finally:
        os.chdir(original_cwd)

    assert len(calls) == 1
    assert calls[0]["script_location"] == script_location, (
        f"FAIL: script_location changed when run from a different CWD -- "
        f"this is exactly the bug being fixed. got {calls[0]['script_location']!r}, expected {script_location!r}"
    )
    print("PASS: resolved script_location is identical no matter where the process was started from\n")

    print("ALL TESTS PASSED")


run()
