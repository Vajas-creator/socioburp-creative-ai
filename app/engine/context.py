"""
Plain-data snapshot of a business + its brand profile. Every engine module
(prompt_builder, caption) takes this instead of live SQLAlchemy ORM objects,
because those objects become unusable (DetachedInstanceError) the moment
the DB session that loaded them closes — and the engine pipeline spans
multiple session-scoped blocks plus slow external API calls in between.

Build one of these ONCE, inside the `with get_session()` block, then pass
it freely through the rest of the pipeline.
"""
from dataclasses import dataclass


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

    @property
    def has_logo(self) -> bool:
        return bool(self.logo_url)
