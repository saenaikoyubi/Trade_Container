from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import aliased
from sqlalchemy.orm import Session

from .market_rules import TERMINAL_ORDER_STATUSES
from .models import CloseRequest, ControlFlag, DailyPnl, Fill, Order, Position, ServiceHeartbeat


ACTIVE_STATUSES = ("pending", "open", "partially_filled")


def claim_order(session: Session, *, now: datetime | None = None) -> Order | None:
    current_time = now or datetime.now(timezone.utc)
    stale_processing = current_time - timedelta(seconds=30)
    earlier = aliased(Order)
    earlier_active = select(earlier.id).where(
        earlier.close_position_id == Order.close_position_id,
        earlier.close_sequence < Order.close_sequence,
        earlier.status.not_in(TERMINAL_ORDER_STATUSES),
    ).exists()
    order = session.scalar(
        select(Order)
        .where(
            Order.next_attempt_at <= current_time,
            or_(
                Order.status.in_(ACTIVE_STATUSES),
                (Order.status == "processing") & (Order.updated_at < stale_processing),
            ),
            or_(Order.close_position_id.is_(None), ~earlier_active),
        )
        .order_by(Order.next_attempt_at, Order.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if order is None:
        return None
    order.status = "processing"
    order.updated_at = current_time
    session.flush()
    return order


def record_fill(
    session: Session,
    order: Order,
    *,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    liquidity_role: str = "taker",
    market_data_id: str | None = None,
    executed_at: datetime | None = None,
) -> Fill:
    control = session.get(ControlFlag, 1, with_for_update=True)
    if control is not None and control.kill_switch:
        raise RuntimeError("kill switch is enabled")
    if order.cancellation_requested:
        raise RuntimeError("order cancellation was requested")
    if order.close_request_id is not None:
        parent = session.get(CloseRequest, order.close_request_id, with_for_update=True)
        if parent is None or parent.status in {"canceling", "canceled", "failed", "completed"}:
            raise RuntimeError("close request is no longer active")
    if quantity <= 0 or quantity > Decimal(order.quantity) - Decimal(order.filled_quantity):
        raise RuntimeError("fill quantity exceeds the remaining order")
    if order.reduce_only:
        position = session.scalar(select(Position).where(
            Position.exchange_id == order.exchange_id, Position.symbol == order.symbol
        ).with_for_update())
        current_quantity = Decimal(position.quantity) if position is not None else Decimal("0")
        signed_fill = quantity if order.side == "buy" else -quantity
        if current_quantity == 0 or current_quantity * signed_fill >= 0 or quantity > abs(current_quantity):
            raise RuntimeError("reduce-only fill would increase or reverse the position")
    timestamp = executed_at or datetime.now(timezone.utc)
    sequence = len(session.scalars(select(Fill).where(Fill.order_id == order.id)).all()) + 1
    fill = Fill(
        order_id=order.id,
        sequence=sequence,
        exchange_id=order.exchange_id,
        symbol=order.symbol,
        side=order.side,
        quantity=quantity,
        price=price,
        fee=fee,
        liquidity_role=liquidity_role,
        market_data_id=market_data_id,
        executed_at=timestamp,
    )
    session.add(fill)
    previous_filled = Decimal(order.filled_quantity)
    previous_notional = (Decimal(order.average_fill_price) if order.average_fill_price else Decimal("0")) * previous_filled
    order.filled_quantity = previous_filled + quantity
    order.average_fill_price = (previous_notional + price * quantity) / order.filled_quantity
    order.total_fee = Decimal(order.total_fee) + fee
    _update_position(
        session,
        order.exchange_id,
        order.symbol,
        order.side,
        quantity,
        price,
        fee,
        executed_at=timestamp,
    )
    return fill


def _update_position(
    session: Session,
    exchange_id: str,
    symbol: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    fee: Decimal,
    *,
    executed_at: datetime,
) -> None:
    position = session.scalar(
        select(Position)
        .where(Position.exchange_id == exchange_id, Position.symbol == symbol)
        .with_for_update()
    )
    if position is None:
        position = Position(exchange_id=exchange_id, symbol=symbol)
        session.add(position)
        session.flush()

    old_quantity = Decimal(position.quantity)
    old_average = Decimal(position.average_entry_price)
    signed_fill = quantity if side == "buy" else -quantity
    new_quantity = old_quantity + signed_fill

    pnl_delta = -fee
    if old_quantity == 0 or old_quantity * signed_fill > 0:
        total = abs(old_quantity) + quantity
        position.average_entry_price = ((abs(old_quantity) * old_average) + (quantity * price)) / total
    else:
        closed = min(abs(old_quantity), quantity)
        direction = Decimal("1") if old_quantity > 0 else Decimal("-1")
        closing_pnl = closed * (price - old_average) * direction
        position.realized_pnl = Decimal(position.realized_pnl) + closing_pnl
        pnl_delta += closing_pnl
        if new_quantity == 0:
            position.average_entry_price = Decimal("0")
        elif old_quantity * new_quantity < 0:
            position.average_entry_price = price

    position.quantity = new_quantity
    position.realized_pnl = Decimal(position.realized_pnl) - fee
    today = executed_at.astimezone(timezone.utc).date()
    daily = session.scalar(select(DailyPnl).where(DailyPnl.trade_date == today).with_for_update())
    if daily is None:
        daily = DailyPnl(trade_date=today)
        session.add(daily)
        session.flush()
    daily.realized_pnl = Decimal(daily.realized_pnl) + pnl_delta


def heartbeat(session: Session, service: str, healthy: bool = True, detail: str | None = None) -> None:
    item = session.get(ServiceHeartbeat, service)
    if item is None:
        item = ServiceHeartbeat(service=service)
        session.add(item)
    item.healthy = healthy
    item.detail = detail
    item.updated_at = datetime.now(timezone.utc)
