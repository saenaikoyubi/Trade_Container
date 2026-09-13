from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    request_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    strategy_id: Mapped[str | None] = mapped_column(String(64), index=True)
    exchange_id: Mapped[str] = mapped_column(String(32), index=True, default="unknown", nullable=False)
    exchange_network: Mapped[str] = mapped_column(String(16), default="mainnet", nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    status: Mapped[str] = mapped_column(String(32), index=True, default="pending", nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    filled_quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), default=Decimal("0"), nullable=False)
    average_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(36, 18))
    total_fee: Mapped[Decimal] = mapped_column(Numeric(36, 18), default=Decimal("0"), nullable=False)
    cancellation_requested: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_market_data_id: Mapped[str | None] = mapped_column(String(64))
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("order_id", "sequence", name="uq_fill_order_sequence"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id", ondelete="CASCADE"), index=True, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    exchange_id: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    fee: Mapped[Decimal] = mapped_column(Numeric(36, 18), nullable=False)
    liquidity_role: Mapped[str] = mapped_column(String(8), default="taker", nullable=False)
    market_data_id: Mapped[str | None] = mapped_column(String(64))
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Position(Base):
    __tablename__ = "positions"
    __table_args__ = (
        UniqueConstraint("exchange_id", "symbol", name="uq_position_exchange_symbol"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    exchange_id: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(36, 18), default=Decimal("0"), nullable=False)
    average_entry_price: Mapped[Decimal] = mapped_column(Numeric(36, 18), default=Decimal("0"), nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), default=Decimal("0"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ControlFlag(Base):
    __tablename__ = "control_flags"
    __table_args__ = (
        CheckConstraint(
            "NOT (kill_switch AND close_only)",
            name="ck_control_flags_exclusive_modes",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    close_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class DailyPnl(Base):
    __tablename__ = "daily_pnl"
    __table_args__ = (UniqueConstraint("trade_date", name="uq_daily_pnl_date"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(Numeric(36, 18), default=Decimal("0"), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ServiceHeartbeat(Base):
    __tablename__ = "service_heartbeats"

    service: Mapped[str] = mapped_column(String(64), primary_key=True)
    healthy: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
