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
    learned_preferences: list[str] = field(default_factory=list)  # past accepted requests, for style/direction reference
    style_summary: str | None = None  # distilled synthesis once learned_preferences has cycled a few times
    language: str = "en"  # 'en'|'hi'|'hinglish'|'ta'|'te'|'kn'|'ml' — see app/i18n.py
    industry_style: str | None = None  # cached industry-wide trend research, see app/engine/industry_research.py

    @property
    def has_logo(self) -> bool:
        return bool(self.logo_url)
