"""
SocioBurp Creative AI — FastAPI entry point.

Run locally:
    uvicorn app.main:app --reload --port 8000

Deploy: this file is the target for Render's start command
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from app import network_fix  # noqa: F401  -- MUST be the first import. Forces IPv4-only DNS before any other module can open a network connection. See app/network_fix.py.

import logging

from fastapi import FastAPI

from app.whatsapp.webhook import router as whatsapp_router
from app.payments import router as payments_router
from app.db import init_db, run_migrations

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("socioburp")

app = FastAPI(title="SocioBurp Creative AI")

app.include_router(whatsapp_router, tags=["whatsapp"])
app.include_router(payments_router, tags=["payments"])


@app.on_event("startup")
async def on_startup():
    logger.info("Starting SocioBurp Creative AI backend...")
    # Must run BEFORE init_db() and before FastAPI/uvicorn starts accepting
    # requests -- Render's free plan has no Shell or Pre-Deploy Command, so
    # this is the only place migrations can run ahead of live traffic. See
    # app/db.py's run_migrations() docstring for why this replaced the
    # previous "run it by hand on Render" process.
    run_migrations()
    init_db()
    logger.info("Startup complete.")


@app.get("/")
async def health():
    """Simple health check — also what you hit to confirm Render deployed OK."""
    return {"status": "ok", "service": "socioburp-creative-ai"}
