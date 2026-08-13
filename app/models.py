"""
ORM models. Mirrors the schema in migrations/versions/0001_initial.py —
if you change one, change the other.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Text, ForeignKey, TIMESTAMP, func, Boolean
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship

from app.db import Base


def gen_uuid():
    return uuid.uuid4()


class Business(Base):
    __tablename__ = "businesses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    phone = Column(String(20), unique=True, nullable=False, index=True)  # WhatsApp number = identity
    name = Column(String(200))  # best-effort, extracted from their business-description answer; often NULL -- see app/onboarding.py
    owner_name = Column(Text, nullable=True)  # the client's own name, asked once during onboarding -- see app/onboarding.py's "awaiting_owner_name" state. Preferred over `name` (the business name) for addressing them personally, e.g. app/router.py's bare-greeting reply
    industry = Column(String(100))  # free text now (e.g. "handmade gifting business"), not a fixed category -- see app/onboarding.py
    onboarding_state = Column(String(50), default="new")  # new -> awaiting_owner_name -> awaiting_business_description -> awaiting_instagram -> done
    instagram_account_id = Column(String(50), nullable=True)  # Meta IG Business Account ID; NULL = not onboarded for auto-posting (auto-POSTING -- separate from instagram_handle below, which is just what the client told us during onboarding)
    instagram_handle = Column(Text, nullable=True)  # whatever the client sent when asked for their Instagram page (handle, link, or just left as text) -- see app/onboarding.py's "awaiting_instagram" state
    regen_allowance_this_cycle = Column(Integer, nullable=False, default=0)  # quality-check regens earned by credits purchased
    regens_used_this_cycle = Column(Integer, nullable=False, default=0)  # quality-check regens actually used
    preferred_language = Column(String(10), nullable=True)  # 'en'|'hi'|'hinglish'|'ta'|'te'|'kn'|'ml'; NULL = not yet detected, treated as 'en'
    # Set when the very first message already described a real creative
    # request (see app/onboarding.py's "new" state) instead of just being a
    # greeting -- the client's own words, carried through the question
    # sequence and auto-generated the moment onboarding finishes, so they
    # never have to repeat themselves. Cleared once that generation runs.
    pending_first_request = Column(Text, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    brand_profile = relationship("BrandProfile", back_populates="business", uselist=False)
    generations = relationship("Generation", back_populates="business")
    conversation_state = relationship("ConversationState", back_populates="business", uselist=False)


class BrandProfile(Base):
    __tablename__ = "brand_profiles"

    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), primary_key=True)
    logo_url = Column(Text)
    primary_color = Column(String(7))
    secondary_color = Column(String(7))
    tone = Column(String(50))  # premium / friendly / bold / minimal
    target_audience = Column(String(200))
    website = Column(String(200))
    contact_phone = Column(String(20))
    address = Column(Text)
    extras = Column(JSONB, default=dict)  # products, offers, anything else

    # Fetched via the "SocioBurp — Instagram Profile Fetch" Make.com scenario
    # (Business Discovery API, using SocioBurp's own connected IG account —
    # no OAuth needed from the client). Populated best-effort, in the
    # background, from Business.instagram_handle -- see
    # app/engine/instagram_analysis.py. Both stay NULL if the fetch hasn't
    # run yet, failed, or the handle isn't a public Business/Creator account.
    instagram_bio = Column(Text, nullable=True)
    instagram_recent_captions = Column(Text, nullable=True)  # newline-joined, most recent first

    business = relationship("Business", back_populates="brand_profile")


class Generation(Base):
    __tablename__ = "generations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), index=True)
    user_message = Column(Text, nullable=False)
    built_prompt = Column(Text)
    image_url = Column(Text)
    base_image_url = Column(Text, nullable=True)  # pre-composite background — lets logo-move revisions re-paste for free
    caption = Column(Text)
    hashtags = Column(Text)
    quality_score = Column(Integer)
    credits_charged = Column(Integer, default=1)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("generations.id"), nullable=True)
    status = Column(String(20), default="pending")  # pending -> generating -> done -> failed -> blocked (budget cap hit)
    posted_to_instagram = Column(Boolean, nullable=False, default=False)
    # How this generation got triggered: 'specific_enough' | 'proposal_confirmed' |
    # 'adjust_ready' (the client's ADJUST reply was already specific enough
    # to skip re-proposing and waiting for a separate confirm -- see
    # concept_proposal.interpret_reply()'s ready_to_generate) | 'adjust_cap' |
    # 'revision' | 'logo_free_revision' | 'onboarding_complete' (auto-triggered
    # the moment onboarding finishes, bypassing the concept-proposal gate --
    # see app/onboarding.py) | 'image_intent' | 'image_intent_as_is'
    # (see app/engine/image_intent.py) | 'carousel' (see below). Nullable
    # for rows created before this column existed.
    trigger_source = Column(String(30), nullable=True)
    # Set only for a carousel post (trigger_source='carousel') -- ordered
    # list of each slide's R2 URL. image_url above still holds the FIRST
    # slide (used as the WhatsApp preview thumbnail); NULL for a normal,
    # single-image generation. See app/engine/orchestrator.py's
    # generate_carousel() and app/instagram.py's posting branch.
    carousel_image_urls = Column(JSONB, nullable=True)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())

    business = relationship("Business", back_populates="generations")


class CreditLedger(Base):
    __tablename__ = "credit_ledger"

    id = Column(Integer, primary_key=True, autoincrement=True)
    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), index=True)
    delta = Column(Integer, nullable=False)  # +200 top-up, -1 generation
    reason = Column(String(50), nullable=False)  # signup_bonus / generation / topup / refund
    ref_id = Column(String(64), nullable=True)  # generation id (as str) or Razorpay payment_link id
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class ConversationState(Base):
    __tablename__ = "conversation_state"

    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), primary_key=True)
    last_generation_id = Column(UUID(as_uuid=True), nullable=True)
    pending_proposal = Column(Text, nullable=True)
    # JSON-in-Text, same pattern as pending_proposal above -- tracks an
    # in-progress carousel negotiation (slide count, then per-slide
    # content) across multiple incoming messages. See app/engine/carousel.py.
    pending_carousel = Column(Text, nullable=True)
    # Tracks an in-progress "what should I do with this uploaded photo"
    # negotiation -- see app/engine/image_intent.py.
    pending_image_intent = Column(Text, nullable=True)
    context = Column(JSONB, default=dict)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    business = relationship("Business", back_populates="conversation_state")


class LearningEvent(Base):
    """
    Audit trail for app/engine/learning.py's record_accepted_direction().
    Written on every call, regardless of outcome — lets the weekly
    instrumentation query precisely compare "quality_score of the NEXT
    generation after a real 'recorded' event" vs "...after a 'skipped_quality'
    event", instead of a fuzzier proxy correlation over the generations
    table alone.
    """
    __tablename__ = "learning_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), index=True)
    generation_id = Column(UUID(as_uuid=True), ForeignKey("generations.id"), nullable=True)
    event_type = Column(String(20), nullable=False)  # 'recorded' | 'skipped_quality' | 'skipped_no_profile' | 'skipped_free_revision' | 'distilled'
    quality_score = Column(Integer, nullable=True)  # the generation's score at the time of this event, if applicable
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class AnalyticsEvent(Base):
    """
    Activation-funnel instrumentation, per business. Deliberately separate
    from LearningEvent (which is specifically about the accept/skip signal
    for the learning loop) -- this is general product analytics: signup,
    onboarding_completed, first_creative_approved, user_returned_voluntarily.
    See app/analytics.py for the write side and the definition of each
    event_type, especially user_returned_voluntarily (a heuristic, not a
    literal session boundary -- there's no session concept in this app).
    """
    __tablename__ = "analytics_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), index=True)
    event_type = Column(String(50), nullable=False, index=True)  # 'signup' | 'onboarding_completed' | 'first_creative_approved' | 'user_returned_voluntarily'
    event_metadata = Column(JSONB, nullable=True)  # optional free-form context, e.g. {"industry": "bakery"}
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())


class IndustryStyleResearch(Base):
    """
    Shared, cached research per industry (NOT per business) — a one-time
    web-search-backed pass distilling current visual/marketing trends for
    that industry, reused by every business in it rather than re-run per
    client. See app/engine/industry_research.py.
    """
    __tablename__ = "industry_style_research"

    industry = Column(String(100), primary_key=True)
    style_summary = Column(Text, nullable=False)
    created_at = Column(TIMESTAMP(timezone=True), server_default=func.now())
