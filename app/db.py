"""
SQLAlchemy engine + session. We use the ORM for writes and raw SQL for the
credit balance aggregate (clearer than an ORM sum query).
"""
import logging
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

from app.config import settings

logger = logging.getLogger("socioburp.db")

# Render/Neon Postgres URLs sometimes start with postgres:// — SQLAlchemy needs postgresql://
db_url = settings.DATABASE_URL
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

engine = create_engine(db_url, pool_pre_ping=True, pool_size=5, max_overflow=10)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


@contextmanager
def get_session():
    """Use as: with get_session() as db: ..."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def run_migrations():
    """
    Runs `alembic upgrade head` programmatically, at application startup,
    before the app accepts any traffic.

    Render's free plan has no Shell access and no Pre-Deploy Command (both
    premium-only) — there is no out-of-band way to run migrations before a
    deploy goes live. This is the only hook left that runs before the
    server starts serving requests. It's synchronous/blocking by design:
    if a migration fails, startup should fail loudly rather than serve
    traffic against a database schema the code doesn't match — that
    mismatch (a column present in the ORM model but not yet in Postgres)
    is exactly the outage this replaces. See migration 0010's rollout.

    script_location is set explicitly to an absolute path rather than left
    as alembic.ini's relative "migrations" — Alembic resolves a relative
    script_location against the process's current working directory, not
    against alembic.ini's own location, so this would silently break if
    the app is ever started from a directory other than the repo root.
    """
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    repo_root = Path(__file__).resolve().parent.parent
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("script_location", str(repo_root / "migrations"))

    logger.info("Running alembic upgrade head...")
    command.upgrade(cfg, "head")
    logger.info("Database schema up to date.")


def init_db():
    """
    Creates tables if they don't exist. Fine for MVP bootstrap; once you have
    real data, switch fully to `alembic upgrade head` and remove this call
    from main.py's startup event so you don't accidentally bypass migrations.
    """
    import app.models  # noqa: F401 — ensures models are registered on Base
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ensured.")
