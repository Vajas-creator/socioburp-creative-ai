"""
Placeholder for Week 2. Full implementation will:
  1. Detect intent (GENERATE vs REVISE vs QUESTION) via Claude Haiku
  2. Build a detailed image prompt via Claude Sonnet + brand profile
  3. Call the image model (provider chosen after the 20-prompt benchmark)
  4. Composite the logo onto the result
  5. Generate caption + hashtags via Claude
  6. Run the quality checker, regenerate once if score < 60
  7. Upload to R2, save the Generation row, charge 1 credit
  8. Send the image + caption back on WhatsApp

Stubbed now so app.router can import without errors during Week 1 testing.
"""
import logging
import uuid

from app.whatsapp.client import send_text

logger = logging.getLogger("socioburp.engine")


async def generate(business_id: uuid.UUID, msg):
    await send_text(
        msg.sender,
        "🎨 Creative generation is coming very soon! Your account and credits "
        "are all set up — we'll notify you the moment this feature goes live.",
    )
