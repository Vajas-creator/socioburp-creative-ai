"""
SocioBurp Creative AI — FastAPI entry point.

Run locally:
    uvicorn app.main:app --reload --port 8000

Deploy: this file is the target for Render's start command
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
import logging

from fastapi import FastAPI

from app.whatsapp.webhook import router as whatsapp_router
from app.payments import router as payments_router
from app.instagram_oauth import router as instagram_router
from app.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("socioburp")

app = FastAPI(title="SocioBurp Creative AI")

app.include_router(whatsapp_router, tags=["whatsapp"])
app.include_router(payments_router, tags=["payments"])
app.include_router(instagram_router, tags=["instagram"])


@app.on_event("startup")
async def on_startup():
    logger.info("Starting SocioBurp Creative AI backend...")
    init_db()
    logger.info("Startup complete.")


@app.get("/")
async def health():
    """Simple health check — also what you hit to confirm Render deployed OK."""
    return {"status": "ok", "service": "socioburp-creative-ai"}
