from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import ExchangeSettings, Settings
from .models import ControlFlag, DailyPnl, Order, Position
from .market_rules import positive_decimal


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str | None = None
    temporary: bool = False
    reason_code: str | None = None


def instrument_preflight_reason(instrument: dict, order_type: str) -> str | None:
    """Conservative metadata-only eligibility shared by the market-info API."""
    status = instrument.get("status")
    if status == "unknown" or status is None:
        return "instrument_metadata_invalid"
    if status != "active":
        return "instrument_not_tradable"
    if instrument.get("metadata_stale"):
        return "instrument_metadata_stale"
    invalid = set(instrument.get("_invalid_fields") or ())
    if (positive_decimal(instrument.get("min_qty")) is None
            or positive_decimal(instrument.get("qty_step")) is None
            or any(name in invalid for name in ("min_qty", "qty_step", "min_notional", "max_qty"))
            or (instrument.get("min_notional") is not None and positive_decimal(instrument.get("min_notional")) is None)
            or (instrument.get("max_qty") is not None and positive_decimal(instrument.get("max_qty")) is None)):
        return "instrument_metadata_invalid"
    exchange_id = instrument.get("exchange_id")
    if order_type == "limit" and (exchange_id in {"bybit", "dydx"} or "price_step" in invalid):
        if "price_step" in invalid or positive_decimal(instrument.get("price_step")) is None:
            return "instrument_metadata_invalid"
    if order_type == "market" and exchange_id == "bybit":
        if "max_market_qty" in invalid or positive_decimal(instrument.get("max_market_qty")) is None:
            return "instrument_metadata_invalid"
    return None


def validate_instrument_market(
    instrument: dict,
    *,
    exchange_id: str,
    order_type: str,
    reduce_only: bool,
    position_quantity: Decimal,
    side: str,
    quantity: Decimal,
) -> RiskDecision:
    if instrument.get("metadata_stale") and not reduce_only:
        return RiskDecision(False, "instrument metadata is stale", True, "instrument_metadata_stale")
    if exchange_id == "bybit":
        market_fields = (
            ("contract_type", "linear_perpetual"),
            ("quote_asset", "USDT"),
            ("settle_asset", "USDT"),
        )
        if any(instrument.get(field) not in {None, "unknown", expected} for field, expected in market_fields):
            return RiskDecision(False, "unsupported Bybit market", reason_code="unsupported_market")
        if any(instrument.get(field) in {None, "unknown"} for field, _ in market_fields):
            return RiskDecision(False, "Bybit market metadata is incomplete", True, "instrument_metadata_invalid")
    signed = quantity if side == "buy" else -quantity
    if reduce_only and (
        position_quantity == 0
        or position_quantity * signed >= 0
        or quantity > abs(position_quantity)
    ):
        return RiskDecision(False, "reduce-only order would increase or reverse the position")
    status = str(instrument.get("status") or "unknown")
    if exchange_id == "bybit" and status != "active":
        if reduce_only and order_type == "market":
            return RiskDecision(True)
        if status == "unknown":
            return RiskDecision(False, "instrument trading status is unknown", True, "instrument_metadata_invalid")
        return RiskDecision(False, "instrument is not tradable", reason_code="instrument_not_tradable")
    if exchange_id != "bybit" and status == "inactive":
        return RiskDecision(False, "instrument is inactive")
    return RiskDecision(True)


def validate_instrument_quantity(
    quantity: Decimal,
    price: Decimal | None,
    instrument: dict,
    *,
    is_full_close: bool = False,
    is_close_child: bool = False,
    order_type: str = "limit",
    previously_filled: bool = False,
) -> RiskDecision:
    if quantity <= 0:
        return RiskDecision(False, "quantity must be positive")
    invalid_fields = set(instrument.get("_invalid_fields") or ())
    max_qty = instrument.get("max_qty")
    if "max_qty" in invalid_fields or (max_qty is not None and positive_decimal(max_qty) is None):
        return RiskDecision(False, "max_qty is invalid", True, "instrument_metadata_invalid")
    if max_qty is not None and quantity > Decimal(max_qty):
        return RiskDecision(False, "maximum instrument quantity exceeded")
    if order_type == "market":
        max_market_qty = instrument.get("max_market_qty")
        if instrument.get("exchange_id") == "bybit" and (
            max_market_qty is None or positive_decimal(max_market_qty) is None
            or "max_market_qty" in invalid_fields
        ):
            return RiskDecision(False, "max_market_qty is missing or invalid", True, "instrument_metadata_invalid")
        if max_market_qty is not None and positive_decimal(max_market_qty) is None:
            return RiskDecision(False, "max_market_qty is invalid", True, "instrument_metadata_invalid")
        if max_market_qty is not None and quantity > Decimal(max_market_qty):
            return RiskDecision(False, "maximum instrument quantity exceeded")
    if is_full_close or is_close_child or previously_filled:
        return RiskDecision(True)

    min_qty = instrument.get("min_qty")
    qty_step = instrument.get("qty_step")
    min_notional = instrument.get("min_notional")
    if min_qty is None or qty_step is None or "min_qty" in invalid_fields or "qty_step" in invalid_fields:
        return RiskDecision(False, "instrument metadata is incomplete", True, "instrument_metadata_invalid")
    min_qty = positive_decimal(min_qty)
    qty_step = positive_decimal(qty_step)
    if min_qty is None or qty_step is None:
        return RiskDecision(False, "instrument metadata is invalid", True, "instrument_metadata_invalid")
    if min_notional is not None:
        min_notional = positive_decimal(min_notional)
    if "min_notional" in invalid_fields or (instrument.get("min_notional") is not None and min_notional is None):
        return RiskDecision(False, "min_notional is invalid", True, "instrument_metadata_invalid")
    if quantity < min_qty:
        return RiskDecision(False, "minimum instrument quantity not met")
    if (quantity - min_qty) % qty_step != 0:
        return RiskDecision(False, "instrument quantity step is not aligned")
    if min_notional is not None and price is None:
        return RiskDecision(False, "market price is unavailable", True, "order_book_unavailable")
    if min_notional is not None and price is not None and quantity * price < min_notional:
        return RiskDecision(False, "minimum instrument notional not met")
    return RiskDecision(True)


def validate_limit_price(price: Decimal | None, instrument: dict, *, exchange_id: str) -> RiskDecision:
    if price is None:
        return RiskDecision(False, "limit price is required")
    if not price.is_finite() or price <= 0:
        return RiskDecision(False, "limit price must be positive and finite")
    invalid_fields = set(instrument.get("_invalid_fields") or ())
    step = positive_decimal(instrument.get("price_step"))
    if "price_step" in invalid_fields or (step is None and exchange_id in {"bybit", "dydx"}):
        return RiskDecision(False, "price_step is missing or invalid", True, "instrument_metadata_invalid")
    if step is not None and price % step != 0:
        return RiskDecision(False, "limit price step is not aligned", reason_code="price_step_not_aligned")
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
    valuation_price: Decimal | None = None,
) -> RiskDecision:
    if order.exchange_id != exchange_config.exchange_id:
        return RiskDecision(False, "order exchange does not match exchange configuration")
    if order.exchange_id != "bybit" and order.symbol not in exchange_config.symbols:
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
    if order.reduce_only and (
        current_quantity == 0 or current_quantity * signed_order >= 0
        or remaining_quantity > abs(Decimal(current_quantity))
    ):
        return RiskDecision(False, "reduce-only order would increase or reverse the position")
    if instrument is not None:
        if order.order_type == "limit":
            price_decision = validate_limit_price(order.limit_price, instrument, exchange_id=order.exchange_id)
            if not price_decision.allowed:
                return price_decision
        market_decision = validate_instrument_market(
            instrument, exchange_id=order.exchange_id, order_type=order.order_type,
            reduce_only=order.reduce_only, position_quantity=Decimal(current_quantity),
            side=order.side, quantity=remaining_quantity,
        )
        if not market_decision.allowed:
            return market_decision

    is_full_close = bool(order.reduce_only and remaining_quantity == abs(Decimal(current_quantity)))
    if instrument is not None:
        instrument_decision = validate_instrument_quantity(
            remaining_quantity,
            price,
            instrument,
            is_full_close=is_full_close,
            is_close_child=order.close_request_id is not None,
            order_type=order.order_type,
            previously_filled=Decimal(order.filled_quantity) > 0,
        )
        if not instrument_decision.allowed:
            return instrument_decision

    protected_close = (close_only and order.reduce_only) or order.close_request_id is not None
    if not protected_close:
        if order.quantity > config.max_order_quantity:
            return RiskDecision(False, "maximum order quantity exceeded")
        since = datetime.now(timezone.utc) - timedelta(minutes=1)
        recent_count = session.scalar(select(func.count(Order.id)).where(Order.created_at >= since)) or 0
        if recent_count > config.max_orders_per_minute:
            return RiskDecision(False, "order rate limit exceeded")

    projected_notional = abs(current_quantity + signed_order) * (valuation_price or mid_price)
    if not protected_close and projected_notional > config.max_position_notional:
        return RiskDecision(False, "maximum position notional exceeded")

    daily = session.scalar(select(DailyPnl).where(DailyPnl.trade_date == datetime.now(timezone.utc).date()))
    if not protected_close and daily and Decimal(daily.realized_pnl) <= -config.max_daily_loss:
        return RiskDecision(False, "maximum daily loss reached")
    return RiskDecision(True)
