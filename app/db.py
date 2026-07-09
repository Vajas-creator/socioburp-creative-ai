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


def init_db():
    """
    Creates tables if they don't exist. Fine for MVP bootstrap; once you have
    real data, switch fully to `alembic upgrade head` and remove this call
    from main.py's startup event so you don't accidentally bypass migrations.
    """
    import app.models  # noqa: F401 — ensures models are registered on Base
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables ensured.")
