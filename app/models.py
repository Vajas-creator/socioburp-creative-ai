"""
ORM models. Mirrors the schema in migrations/versions/0001_initial.py —
if you change one, change the other.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Text, ForeignKey, TIMESTAMP, func
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
    name = Column(String(200))
    industry = Column(String(100))
    onboarding_state = Column(String(50), default="new")  # new -> name -> industry -> logo -> colors -> tone -> done
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

    business = relationship("Business", back_populates="brand_profile")


class Generation(Base):
    __tablename__ = "generations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=gen_uuid)
    business_id = Column(UUID(as_uuid=True), ForeignKey("businesses.id"), index=True)
    user_message = Column(Text, nullable=False)
    built_prompt = Column(Text)
    image_url = Column(Text)
    caption = Column(Text)
    hashtags = Column(Text)
    quality_score = Column(Integer)
    credits_charged = Column(Integer, default=1)
    parent_id = Column(UUID(as_uuid=True), ForeignKey("generations.id"), nullable=True)
    status = Column(String(20), default="pending")  # pending -> generating -> done -> failed
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
    context = Column(JSONB, default=dict)
    updated_at = Column(TIMESTAMP(timezone=True), server_default=func.now(), onupdate=func.now())

    business = relationship("Business", back_populates="conversation_state")
