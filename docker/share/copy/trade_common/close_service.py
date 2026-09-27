from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .market_rules import ACTIVE_CLOSE_STATUSES, TERMINAL_ORDER_STATUSES
from .models import CloseRequest, CloseRequestPosition, Order, Position, RequestKey


def all_children(session: Session, parent_id: str) -> list[Order]:
    return list(session.scalars(
        select(Order).where(Order.close_request_id == parent_id).order_by(Order.symbol, Order.close_sequence)
    ).all())


def append_child(
    session: Session, parent: CloseRequest, target: CloseRequestPosition,
    *, quantity: Decimal, network: str,
) -> Order:
    sequence = (session.scalar(select(func.max(Order.close_sequence)).where(Order.close_position_id == target.id)) or 0) + 1
    request_id = f"close-child:{parent.id}:{target.id}:{sequence}"
    order = Order(
        id=str(uuid.uuid4()), request_id=request_id, strategy_id=parent.strategy_id,
        exchange_id=parent.exchange_id, exchange_network=network,
        symbol=target.symbol, side="sell" if target.remaining_position_quantity > 0 else "buy",
        order_type="market", quantity=quantity, reduce_only=True,
        close_request_id=parent.id, close_position_id=target.id, close_sequence=sequence,
    )
    session.add(order)
    session.add(RequestKey(request_id=request_id, operation_kind="order", target_id=order.id))
    session.flush()
    return order


def append_initial_children(
    session: Session, parent: CloseRequest, target: CloseRequestPosition,
    *, quantity: Decimal, cap: Decimal, network: str, incremental: bool,
) -> None:
    """Stage initial children with one flush, even for a large v1 close."""
    pending = []
    remaining = quantity
    sequence = 0
    while remaining > 0:
        sequence += 1
        chunk = min(remaining, cap)
        order_id = str(uuid.uuid4())
        request_id = f"close-child:{parent.id}:{target.id}:{sequence}"
        pending.extend((
            Order(
                id=order_id, request_id=request_id, strategy_id=parent.strategy_id,
                exchange_id=parent.exchange_id, exchange_network=network,
                symbol=target.symbol, side="sell" if target.remaining_position_quantity > 0 else "buy",
                order_type="market", quantity=chunk, reduce_only=True,
                close_request_id=parent.id, close_position_id=target.id, close_sequence=sequence,
            ),
            RequestKey(request_id=request_id, operation_kind="order", target_id=order_id),
        ))
        remaining -= chunk
        if incremental:
            break
    session.add_all(pending)


def refresh_target(session: Session, target: CloseRequestPosition) -> None:
    position = session.get(Position, target.position_id)
    target.remaining_position_quantity = Decimal(position.quantity) if position is not None else Decimal("0")
    if target.status == "failed":
        return
    active = session.scalar(select(Order.id).where(
        Order.close_position_id == target.id,
        Order.status.not_in(TERMINAL_ORDER_STATUSES),
    ).limit(1)) is not None
    if target.status == "canceling":
        target.status = "canceling" if active else "canceled"
    elif target.remaining_position_quantity == 0:
        target.status = "running" if active else "completed"
        target.reason_code = target.detail = None
    elif active:
        progressing = session.scalar(select(Order.id).where(
            Order.close_position_id == target.id,
            Order.status.in_(("processing", "open", "partially_filled")),
        ).limit(1)) is not None
        target.status = "running" if progressing else "queued"
        target.reason_code = target.detail = None
    else:
        target.status = "waiting"


def refresh_parent(session: Session, parent: CloseRequest) -> None:
    targets = list(session.scalars(
        select(CloseRequestPosition).where(CloseRequestPosition.close_request_id == parent.id)
    ).all())
    has_active_children = session.scalar(select(Order.id).where(
        Order.close_request_id == parent.id,
        Order.status.not_in(TERMINAL_ORDER_STATUSES),
    ).limit(1)) is not None
    if parent.status == "canceling":
        if not has_active_children:
            parent.status = "canceled"
        return
    if not targets or all(item.status == "completed" for item in targets):
        parent.status = "completed"
        parent.reason_code = parent.detail = None
    elif not has_active_children and all(item.status in {"completed", "failed"} for item in targets):
        parent.status = "failed"
        failed = next(item for item in targets if item.status == "failed")
        parent.reason_code, parent.detail = failed.reason_code, failed.detail
    elif any(item.status == "waiting" for item in targets):
        parent.status = "waiting"
        waiting = next(item for item in targets if item.status == "waiting")
        parent.reason_code, parent.detail = waiting.reason_code, waiting.detail
    elif any(item.status == "running" for item in targets):
        parent.status = "running"
        parent.reason_code = parent.detail = None
    elif has_active_children and any(item.status == "failed" for item in targets):
        parent.status = "running"
        parent.reason_code = parent.detail = None
    else:
        parent.status = "queued"
        parent.reason_code = parent.detail = None


def cancel_parent(session: Session, parent: CloseRequest, detail: str) -> None:
    if parent.status not in ACTIVE_CLOSE_STATUSES:
        return
    parent.status = "canceling"
    parent.detail = detail
    targets = list(session.scalars(
        select(CloseRequestPosition).where(CloseRequestPosition.close_request_id == parent.id)
    ).all())
    for target in targets:
        if target.status not in {"completed", "failed", "canceled"}:
            target.status = "canceling"
            target.detail = detail
    session.execute(update(Order).where(
        Order.close_request_id == parent.id,
        Order.status.not_in(TERMINAL_ORDER_STATUSES),
    ).values(cancellation_requested=True))
    session.flush()
    for target in targets:
        refresh_target(session, target)
    refresh_parent(session, parent)


def active_parent_for_position(session: Session, position_id: str) -> CloseRequestPosition | None:
    return session.scalar(select(CloseRequestPosition).where(
        CloseRequestPosition.position_id == position_id,
        CloseRequestPosition.status.in_(ACTIVE_CLOSE_STATUSES),
    ))
