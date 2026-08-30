"""
Native Instagram publishing via Meta's Content Publishing API, using the
same per-business connection stored by app/instagram_insights_oauth.py
(Business.instagram_insights_ig_user_id / instagram_insights_access_token).
That OAuth flow's SCOPES now includes instagram_content_publish alongside
instagram_manage_insights specifically so this module can reuse the same
token -- see that module's docstring.

Now reachable from TWO places, both calling the shared
publish_latest_generation() below rather than duplicating its resolve/
publish/mark-posted/record-learning logic:
  - app/engine/agent_tools.py's post_to_instagram tool (agentic-beta
    allowlisted numbers only).
  - app/router.py's "post_to_instagram" global command (classic pipeline
    -- every other business, added specifically so a Meta App Review
    reviewer messaging from an arbitrary phone number can actually
    exercise the instagram_content_publish grant; see that module's
    handle_post_request() below).

Still deliberately NOT touching, replacing, or interacting with
app/instagram.py's existing "Post to Instagram" WhatsApp BUTTON flow,
which keeps posting exclusively through the Make.com scenario exactly as
before. Both paths can end up publishing the same delivered creative --
whichever the client reaches first sets Generation.posted_to_instagram,
and the other then correctly reports "already posted" instead of
double-posting.

Flow (per Meta's Content Publishing API):
  1. POST /{ig-user-id}/media creates a "container" -- a pending media
     object, not yet visible on Instagram. Meta processes it asynchronously
     (downloading image_url, transcoding, etc.).
  2. Poll GET /{container-id}?fields=status_code until it reports FINISHED
     -- publishing against an IN_PROGRESS container fails.
  3. POST /{ig-user-id}/media_publish with that container's id (as
     `creation_id`) makes it a real, live post.

A carousel is the same shape one level deeper: each item is first created
as its own container (is_carousel_item=true) and must independently reach
FINISHED, THEN a parent container is created with media_type=CAROUSEL and
children=<comma-separated child container ids>, which itself is polled and
published like a single post.
"""
import asyncio
import logging
import uuid

import httpx

from app.config import settings
from app.db import get_session
from app.models import Business, Generation
from app.whatsapp.client import send_text

logger = logging.getLogger("socioburp.engine.instagram_publish")

GRAPH_BASE = f"https://graph.facebook.com/{settings.META_GRAPH_API_VERSION}"

CONTAINER_POLL_INTERVAL_SECONDS = 2.0
CONTAINER_POLL_TIMEOUT_SECONDS = 60.0

CAROUSEL_MIN_ITEMS = 2
CAROUSEL_MAX_ITEMS = 10  # Meta's own limit


class InstagramPublishError(Exception):
    """Any failure in the native publish flow -- not connected, bad input, or a Graph API error at any step."""


def _get_connection(business_id: uuid.UUID) -> tuple[str, str] | None:
    """Returns (ig_user_id, access_token) from the existing Insights OAuth connection, or None if not connected."""
    with get_session() as db:
        biz = db.query(Business).filter(Business.id == business_id).first()
        if biz is None or not biz.instagram_insights_ig_user_id or not biz.instagram_insights_access_token:
            return None
        return biz.instagram_insights_ig_user_id, biz.instagram_insights_access_token


async def _create_container(ig_user_id: str, access_token: str, params: dict) -> str:
    """POST /{ig-user-id}/media -- returns the new container's id."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{ig_user_id}/media",
            data={**params, "access_token": access_token},
        )
    if resp.status_code >= 400:
        logger.error("Instagram media container creation failed: %s | %s", resp.status_code, resp.text[:500])
        raise InstagramPublishError(f"Container creation failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()["id"]


async def _wait_for_container_ready(container_id: str, access_token: str) -> None:
    """
    Polls GET /{container-id}?fields=status_code until FINISHED. Raises on
    an ERROR/EXPIRED status, or if CONTAINER_POLL_TIMEOUT_SECONDS elapses
    without reaching a terminal state.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + CONTAINER_POLL_TIMEOUT_SECONDS

    while True:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GRAPH_BASE}/{container_id}",
                params={"fields": "status_code", "access_token": access_token},
            )
        if resp.status_code >= 400:
            logger.error(
                "Instagram container status check failed for %s: %s | %s",
                container_id, resp.status_code, resp.text[:500],
            )
            raise InstagramPublishError(f"Container status check failed: {resp.status_code} {resp.text[:300]}")

        status_code = resp.json().get("status_code")
        if status_code == "FINISHED":
            return
        if status_code in ("ERROR", "EXPIRED"):
            raise InstagramPublishError(f"Container {container_id} processing failed: status_code={status_code}")

        if loop.time() >= deadline:
            raise InstagramPublishError(
                f"Container {container_id} did not finish processing within "
                f"{CONTAINER_POLL_TIMEOUT_SECONDS}s (last status_code={status_code})"
            )
        await asyncio.sleep(CONTAINER_POLL_INTERVAL_SECONDS)


async def _publish_container(ig_user_id: str, access_token: str, creation_id: str) -> str:
    """POST /{ig-user-id}/media_publish -- returns the published media's id."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            f"{GRAPH_BASE}/{ig_user_id}/media_publish",
            data={"creation_id": creation_id, "access_token": access_token},
        )
    if resp.status_code >= 400:
        logger.error("Instagram media publish failed: %s | %s", resp.status_code, resp.text[:500])
        raise InstagramPublishError(f"Publish failed: {resp.status_code} {resp.text[:300]}")
    return resp.json()["id"]


async def publish_image(business_id: uuid.UUID, image_url: str, caption: str = "") -> str:
    """
    Native single-image post. Returns the published Instagram media id.
    Raises InstagramPublishError if the business hasn't connected via the
    Insights OAuth flow, or on any Graph API failure along the way.
    """
    connection = _get_connection(business_id)
    if connection is None:
        raise InstagramPublishError(f"Business {business_id} has no Instagram connection to publish through")
    ig_user_id, access_token = connection

    try:
        creation_id = await _create_container(ig_user_id, access_token, {"image_url": image_url, "caption": caption})
        await _wait_for_container_ready(creation_id, access_token)
        media_id = await _publish_container(ig_user_id, access_token, creation_id)
    except InstagramPublishError:
        raise
    except Exception:
        logger.exception("Native Instagram image publish raised for business=%s", business_id)
        raise InstagramPublishError("Unexpected error during publish") from None

    logger.info("Native Instagram image published for business=%s media_id=%s", business_id, media_id)
    return media_id


async def publish_carousel(business_id: uuid.UUID, image_urls: list[str], caption: str = "") -> str:
    """
    Native carousel post (2-10 images, Meta's own limit). Returns the
    published Instagram media id. Raises InstagramPublishError if the
    business hasn't connected, the item count is out of range, or on any
    Graph API failure along the way (creating a child, creating the parent,
    or publishing it).
    """
    if not (CAROUSEL_MIN_ITEMS <= len(image_urls) <= CAROUSEL_MAX_ITEMS):
        raise InstagramPublishError(
            f"Carousel needs {CAROUSEL_MIN_ITEMS}-{CAROUSEL_MAX_ITEMS} images, got {len(image_urls)}"
        )

    connection = _get_connection(business_id)
    if connection is None:
        raise InstagramPublishError(f"Business {business_id} has no Instagram connection to publish through")
    ig_user_id, access_token = connection

    try:
        child_ids = []
        for image_url in image_urls:
            child_id = await _create_container(
                ig_user_id, access_token, {"image_url": image_url, "is_carousel_item": "true"},
            )
            # Each child must independently finish processing before it can
            # be referenced in the parent's `children` list -- Meta rejects
            # a parent container built from a still-processing child.
            await _wait_for_container_ready(child_id, access_token)
            child_ids.append(child_id)

        parent_id = await _create_container(
            ig_user_id, access_token,
            {"media_type": "CAROUSEL", "children": ",".join(child_ids), "caption": caption},
        )
        await _wait_for_container_ready(parent_id, access_token)
        media_id = await _publish_container(ig_user_id, access_token, parent_id)
    except InstagramPublishError:
        raise
    except Exception:
        logger.exception("Native Instagram carousel publish raised for business=%s", business_id)
        raise InstagramPublishError("Unexpected error during publish") from None

    logger.info("Native Instagram carousel published for business=%s media_id=%s", business_id, media_id)
    return media_id


async def publish_latest_generation(business_id: uuid.UUID, last_generation_id) -> dict:
    """
    Resolves a business's most recently delivered Generation (single image
    or carousel) and native-publishes it -- the shared implementation
    behind both callers described in this module's docstring, so the
    resolve/publish/mark-posted/record-learning sequence exists in exactly
    one place. Never raises; both callers just branch on the returned
    status and phrase their own reply in whatever voice fits their
    pipeline (plain WhatsApp text for the classic command, a tool_result
    string for Claude to phrase itself for the agentic bot).

    Returns {"status": ..., "detail": str | None}, status one of:
    "nothing_to_post" (last_generation_id is None) | "not_found" (stale/
    foreign id) | "already_posted" | "not_connected" | "failed" | "success".
    """
    if last_generation_id is None:
        return {"status": "nothing_to_post", "detail": None}

    with get_session() as db:
        gen = db.query(Generation).filter(
            Generation.id == last_generation_id,
            Generation.business_id == business_id,  # never let a business post another's creative
        ).first()
        # Pulled into plain values while the session is open -- gen becomes
        # unusable (DetachedInstanceError) once this block exits, same as
        # app/instagram.py's handle_post_request().
        gen_found = gen is not None
        already_posted = gen.posted_to_instagram if gen else None
        image_url = gen.image_url if gen else None
        carousel_image_urls = gen.carousel_image_urls if gen else None
        caption = gen.caption if gen else None
        hashtags = gen.hashtags if gen else None

    if not gen_found:
        return {"status": "not_found", "detail": None}
    if already_posted:
        return {"status": "already_posted", "detail": None}

    full_caption = f"{caption}\n\n{hashtags}"[:2200] if caption else ""

    try:
        if carousel_image_urls:
            await publish_carousel(business_id, carousel_image_urls, caption=full_caption)
        else:
            await publish_image(business_id, image_url, caption=full_caption)
    except InstagramPublishError as exc:
        logger.warning("Native Instagram publish failed for business=%s: %s", business_id, exc)
        if "no Instagram connection" in str(exc):
            return {"status": "not_connected", "detail": None}
        return {"status": "failed", "detail": str(exc)}
    except Exception:
        logger.exception("Native Instagram publish raised unexpectedly for business=%s", business_id)
        return {"status": "failed", "detail": None}

    with get_session() as db:
        gen_row = db.query(Generation).filter(Generation.id == last_generation_id).first()
        gen_row.posted_to_instagram = True

    # Same strong-accept-signal exemption as app/instagram.py's Make.com
    # button flow -- choosing to publish something publicly is a stronger
    # signal than any quality score.
    from app.engine import learning
    await learning.record_accepted_direction(business_id, last_generation_id, require_quality_threshold=False)

    return {"status": "success", "detail": None}


async def handle_post_request(business_id: uuid.UUID, phone: str, last_generation_id) -> None:
    """
    Classic-pipeline entry point for the "post_to_instagram" global
    command -- see app/router.py. Sends the client a plain WhatsApp text
    reply itself (unlike agent_tools.py's version, there's no Claude in
    this pipeline to phrase one).
    """
    result = await publish_latest_generation(business_id, last_generation_id)
    status = result["status"]

    if status == "nothing_to_post":
        await send_text(phone, "There's nothing to post yet 🙏 Generate a creative first, then text 'post to instagram'.")
    elif status == "not_found":
        await send_text(phone, "Couldn't find that creative anymore 🙏 Please generate a new one.")
    elif status == "already_posted":
        await send_text(phone, "That one's already posted to Instagram ✅")
    elif status == "not_connected":
        await send_text(phone, "Your Instagram isn't connected yet 🙏 Text 'connect instagram' to set it up first.")
    elif status == "success":
        await send_text(phone, "Posted directly to your Instagram ✅")
    else:  # "failed"
        await send_text(phone, "Posting to Instagram failed 🙏 No credits affected — please try again.")
