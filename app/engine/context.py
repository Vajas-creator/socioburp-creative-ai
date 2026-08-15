"""
Plain-data snapshot of a business + its brand profile. Every engine module
(prompt_builder, caption) takes this instead of live SQLAlchemy ORM objects,
because those objects become unusable (DetachedInstanceError) the moment
the DB session that loaded them closes — and the engine pipeline spans
multiple session-scoped blocks plus slow external API calls in between.

Build one of these ONCE, inside the `with get_session()` block, then pass
it freely through the rest of the pipeline.
"""
from dataclasses import dataclass, field


@dataclass
class BusinessContext:
    name: str | None
    industry: str | None
    tone: str | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    target_audience: str | None = None
    website: str | None = None
    contact_phone: str | None = None
    logo_url: str | None = None
    logo_position_hint: str | None = None  # free-form client preference, e.g. "put it in the middle" -- see app/engine/logo_capture.py / logo_placement.py
    learned_preferences: list[str] = field(default_factory=list)  # past accepted requests, for style/direction reference
    style_summary: str | None = None  # distilled synthesis once learned_preferences has cycled a few times
    positioning_notes: str | None = None  # price range/positioning + style dos-and-don'ts, from onboarding's "awaiting_brand_details" question -- see app/engine/brand_reflection.py's extract_brand_details()
    language: str = "en"  # 'en'|'hi'|'hinglish'|'ta'|'te'|'kn'|'ml' — see app/i18n.py
    industry_style: str | None = None  # cached industry-wide trend research, see app/engine/industry_research.py
    instagram_handle: str | None = None  # whatever the client typed when asked for their Instagram page
    instagram_bio: str | None = None  # fetched via Make's Business Discovery API, see app/engine/instagram_analysis.py -- None until the background fetch completes (or if it fails/isn't a public Business account)
    instagram_recent_captions: str | None = None  # newline-joined recent post captions, same source

    @property
    def has_logo(self) -> bool:
        return bool(self.logo_url)


async def load_business_context(business_id):
    """
    Loads a BusinessContext + the business's current last_generation_id in
    one go, for callers that need ctx but aren't already inside their own
    `with get_session()` block for some other reason at the same time --
    see app/engine/carousel.py and app/engine/image_intent.py.
    app/engine/orchestrator.py's generate() builds its own inline instead,
    since it also needs that open session for the pending_proposal and
    rate-limit checks running alongside it.

    Imports are function-local, not module-level -- this module is
    imported very widely (almost everything needs BusinessContext), so
    keeping it free of app.db/app.models imports at load time avoids any
    risk of introducing an import cycle.
    """
    from app.db import get_session
    from app.models import Business, BrandProfile, ConversationState
    from app.engine import industry_research

    with get_session() as db:
        business = db.query(Business).filter(Business.id == business_id).first()
        profile = db.query(BrandProfile).filter(BrandProfile.business_id == business_id).first()
        convo = db.query(ConversationState).filter(ConversationState.business_id == business_id).first()
        last_generation_id = convo.last_generation_id if convo else None

        ctx = BusinessContext(
            name=business.name,
            industry=business.industry,
            tone=profile.tone if profile else None,
            primary_color=profile.primary_color if profile else None,
            secondary_color=profile.secondary_color if profile else None,
            target_audience=profile.target_audience if profile else None,
            website=profile.website if profile else None,
            contact_phone=profile.contact_phone if profile else None,
            logo_url=profile.logo_url if profile else None,
            logo_position_hint=(profile.extras or {}).get("logo_position_hint") if profile else None,
            learned_preferences=list((profile.extras or {}).get("learned_preferences", [])) if profile else [],
            style_summary=(profile.extras or {}).get("style_summary") if profile else None,
            positioning_notes=(profile.extras or {}).get("positioning_notes") if profile else None,
            language=business.preferred_language or "en",
            industry_style=industry_research.get_cached_style(business.industry),
            instagram_handle=business.instagram_handle,
            instagram_bio=profile.instagram_bio if profile else None,
            instagram_recent_captions=profile.instagram_recent_captions if profile else None,
        )
        return ctx, last_generation_id
