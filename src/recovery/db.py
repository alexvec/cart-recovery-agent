from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from recovery.config import get_settings

JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JsonType, list[Any]: JsonType}


class Shop(Base):
    __tablename__ = "shops"
    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(255))
    access_token_enc: Mapped[str | None] = mapped_column(String)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("20"))
    min_cart_value_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # None = use economics model
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Checkout(Base):
    """Latest snapshot of a Shopify checkout. The token links it to the eventual order."""

    __tablename__ = "checkouts"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    shop: Mapped[str] = mapped_column(String(255), index=True)
    checkout_id: Mapped[int | None] = mapped_column(BigInteger)
    phone_enc: Mapped[str | None] = mapped_column(String)
    phone_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    country: Mapped[str | None] = mapped_column(String(2))
    first_name_enc: Mapped[str | None] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String(3))
    total_price: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total_price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    line_items: Mapped[list[Any]] = mapped_column(default=list)
    recovery_url: Mapped[str | None] = mapped_column(String)
    consent: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_source: Mapped[str | None] = mapped_column(String(64))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    shopify_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    order_id: Mapped[int | None] = mapped_column(BigInteger)
    order_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))


class WebhookDelivery(Base):
    """Shopify can deliver the same webhook more than once; the delivery id makes processing idempotent."""

    __tablename__ = "webhook_deliveries"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    topic: Mapped[str] = mapped_column(String(64))
    shop: Mapped[str] = mapped_column(String(255))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CallAttempt(Base):
    __tablename__ = "call_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(String(255), index=True)
    shop: Mapped[str] = mapped_column(String(255))
    phone_hash: Mapped[str] = mapped_column(String(64), index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    outcome: Mapped[str | None] = mapped_column(String(32))
    duration_s: Mapped[int | None]
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))


class DoNotCall(Base):
    __tablename__ = "do_not_call"
    shop: Mapped[str] = mapped_column(String(255), primary_key=True)
    phone_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    reason: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RecoveryEvent(Base):
    """Append-only event log: the single source for analytics and audits."""

    __tablename__ = "recovery_events"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    workflow_id: Mapped[str] = mapped_column(String(255), index=True)
    shop: Mapped[str] = mapped_column(String(255), index=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(default=dict)
    agent_version: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


_engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as s, s.begin():
        yield s
