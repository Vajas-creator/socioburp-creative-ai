"""
SocioBurp Creative AI — FastAPI entry point.

Run locally:
    uvicorn app.main:app --reload --port 8000

Deploy: this file is the target for Render's start command
    uvicorn app.main:app --host 0.0.0.0 --port $PORT
"""
from app import network_fix  # noqa: F401  -- MUST be the first import. Forces IPv4-only DNS before any other module can open a network connection. See app/network_fix.py.

import logging

from fastapi import FastAPI, HTTPException

from app.whatsapp.webhook import router as whatsapp_router
from app.payments import router as payments_router
from app.debug_network import router as debug_network_router  # TEMPORARY — remove once the connection issue is resolved
from app.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("socioburp")

app = FastAPI(title="SocioBurp Creative AI")

app.include_router(whatsapp_router, tags=["whatsapp"])
app.include_router(payments_router, tags=["payments"])
app.include_router(debug_network_router, tags=["debug"])  # TEMPORARY


@app.on_event("startup")
async def on_startup():
    logger.info("Starting SocioBurp Creative AI backend...")
    init_db()
    logger.info("Startup complete.")


@app.get("/")
async def health():
    """Simple health check — also what you hit to confirm Render deployed OK."""
    return {"status": "ok", "service": "socioburp-creative-ai"}


@app.get("/debug-anthropic")
def debug_anthropic(secret: str = ""):
    # Gated with the same shared secret as /debug/network-check, fail closed —
    # proxy_env below returns env var VALUES (proxy URLs can embed credentials),
    # so this must never be reachable unauthenticated. See app/debug_network.py.
    import os, httpx, anthropic
    import secrets as _secrets
    from app.config import settings
    if not settings.DEBUG_NETWORK_SECRET or not _secrets.compare_digest(secret, settings.DEBUG_NETWORK_SECRET):
        raise HTTPException(status_code=403, detail="Forbidden")
    out = {"proxy_env": {k: v for k, v in os.environ.items() if "proxy" in k.lower()}}
    try:
        out["httpx"] = httpx.get("https://api.anthropic.com/v1/models", timeout=10).status_code
    except Exception as e:
        out["httpx"] = repr(e)
    try:
        anthropic.Anthropic().messages.create(model="claude-sonnet-4-6", max_tokens=10,
            messages=[{"role": "user", "content": "hi"}])
        out["sdk"] = "OK"
    except anthropic.APIConnectionError as e:
        out["sdk"] = repr(e.__cause__)
    return out
