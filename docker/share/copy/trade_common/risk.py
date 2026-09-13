from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import ExchangeSettings, Settings
from .models import ControlFlag, DailyPnl, Order, Position


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None


def validate_instrument_quantity(
    quantity: Decimal,
    price: Decimal | None,
    instrument: dict,
    *,
    is_full_close: bool = False,
    order_type: str = "limit",
) -> RiskDecision:
    status = str(instrument.get("status") or "unknown")
    if status == "inactive":
        return RiskDecision(False, "instrument is inactive")

    max_qty = instrument.get("max_qty")
    if max_qty is not None and quantity > Decimal(max_qty):
        return RiskDecision(False, "maximum instrument quantity exceeded")
    if order_type == "market":
        max_market_qty = instrument.get("max_market_qty")
        if max_market_qty is not None and quantity > Decimal(max_market_qty):
            return RiskDecision(False, "maximum instrument quantity exceeded")
    if is_full_close:
        return RiskDecision(True)

    min_qty = instrument.get("min_qty")
    qty_step = instrument.get("qty_step")
    min_notional = instrument.get("min_notional")
    if min_qty is None or qty_step is None:
        return RiskDecision(False, "instrument metadata is incomplete")
    min_qty = Decimal(min_qty)
    qty_step = Decimal(qty_step)
    if min_qty <= 0 or qty_step <= 0:
        return RiskDecision(False, "instrument metadata is invalid")
    if min_notional is not None:
        min_notional = Decimal(min_notional)
        if min_notional <= 0:
            return RiskDecision(False, "instrument metadata is invalid")
    if quantity < min_qty:
        return RiskDecision(False, "minimum instrument quantity not met")
    if (quantity - min_qty) % qty_step != 0:
        return RiskDecision(False, "instrument quantity step is not aligned")
    if min_notional is not None and price is not None and quantity * price < min_notional:
        return RiskDecision(False, "minimum instrument notional not met")
    return RiskDecision(True)


def evaluate_order(
    session: Session,
    order: Order,
    mid_price: Decimal,
    config: Settings,
    exchange_config: ExchangeSettings,
    instrument: dict | None = None,
    *,
    execution_price: Decimal | None = None,
) -> RiskDecision:
    if order.exchange_id != exchange_config.exchange_id:
        return RiskDecision(False, "order exchange does not match exchange configuration")
    if order.symbol not in exchange_config.symbols:
        return RiskDecision(False, "symbol is not allowed")
    if order.quantity <= 0:
        return RiskDecision(False, "quantity must be positive")

    flag = session.get(ControlFlag, 1)
    if flag and flag.kill_switch:
        return RiskDecision(False, f"kill switch is enabled: {flag.reason or 'no reason'}")
    close_only = bool(flag and flag.close_only)
    if close_only and not order.reduce_only:
        return RiskDecision(False, f"close-only mode is enabled: {flag.reason or 'no reason'}")

    remaining_quantity = Decimal(order.quantity) - Decimal(order.filled_quantity)
    if remaining_quantity <= 0:
        return RiskDecision(False, "order has no remaining quantity")

    price = order.limit_price or execution_price or mid_price
    notional = remaining_quantity * price

    if order.limit_price is not None and mid_price > 0:
        deviation = abs(order.limit_price - mid_price) / mid_price
        if deviation > config.max_price_deviation_pct:
            return RiskDecision(False, "maximum price deviation exceeded")

    position = session.scalar(
        select(Position).where(
            Position.exchange_id == order.exchange_id,
            Position.symbol == order.symbol,
        )
    )
    current_quantity = position.quantity if position else Decimal("0")
    signed_order = remaining_quantity if order.side == "buy" else -remaining_quantity
    if order.reduce_only:
        if current_quantity == 0 or current_quantity * signed_order > 0:
            return RiskDecision(False, "reduce-only order would increase the position")
        if abs(signed_order) > abs(current_quantity):
            return RiskDecision(False, "reduce-only order would reverse the position")

    is_full_close = bool(order.reduce_only and remaining_quantity == abs(Decimal(current_quantity)))
    if instrument is not None:
        instrument_decision = validate_instrument_quantity(
            remaining_quantity,
            price,
            instrument,
            is_full_close=is_full_close,
            order_type=order.order_type,
        )
        if not instrument_decision.allowed:
            return instrument_decision

    protected_close = close_only and order.reduce_only
    if not protected_close:
        if order.quantity > config.max_order_quantity:
            return RiskDecision(False, "maximum order quantity exceeded")
        if notional > config.max_order_notional:
            return RiskDecision(False, "maximum order notional exceeded")
        since = datetime.now(timezone.utc) - timedelta(minutes=1)
        recent_count = session.scalar(select(func.count(Order.id)).where(Order.created_at >= since)) or 0
        if recent_count > config.max_orders_per_minute:
            return RiskDecision(False, "order rate limit exceeded")

    projected_notional = abs(current_quantity + signed_order) * mid_price
    if not protected_close and projected_notional > config.max_position_notional:
        return RiskDecision(False, "maximum position notional exceeded")

    daily = session.scalar(select(DailyPnl).where(DailyPnl.trade_date == datetime.now(timezone.utc).date()))
    if not protected_close and daily and Decimal(daily.realized_pnl) <= -config.max_daily_loss:
        return RiskDecision(False, "maximum daily loss reached")
    return RiskDecision(True)
