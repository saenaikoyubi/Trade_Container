from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable


@dataclass(frozen=True)
class SimulatedExecution:
    quantity: Decimal
    average_price: Decimal
    notional: Decimal
    fee: Decimal
    fully_filled: bool


def simulate_order(
    *,
    side: str,
    order_type: str,
    quantity: Decimal,
    limit_price: Decimal | None,
    bids: Iterable[Iterable[float | str]],
    asks: Iterable[Iterable[float | str]],
    fee_rate: Decimal,
) -> SimulatedExecution | None:
    if side not in {"buy", "sell"}:
        raise ValueError(f"unsupported side: {side}")
    if order_type not in {"market", "limit"}:
        raise ValueError(f"unsupported order type: {order_type}")
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if order_type == "limit" and limit_price is None:
        raise ValueError("limit price is required")
    if fee_rate < 0:
        raise ValueError("fee rate must not be negative")

    levels = asks if side == "buy" else bids
    remaining = quantity
    notional = Decimal("0")
    filled = Decimal("0")

    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            raise ValueError("order book has an invalid level")
        try:
            price = Decimal(str(level[0]))
            available = Decimal(str(level[1]))
        except (IndexError, InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("order book has an invalid level") from exc
        if not price.is_finite() or not available.is_finite() or price <= 0 or available <= 0:
            raise ValueError("order book has an invalid level")
        if order_type == "limit" and limit_price is not None:
            if side == "buy" and price > limit_price:
                break
            if side == "sell" and price < limit_price:
                break
        take = min(remaining, available)
        filled += take
        notional += take * price
        remaining -= take
        if remaining <= 0:
            break

    if filled <= 0:
        return None
    average = notional / filled
    return SimulatedExecution(
        quantity=filled,
        average_price=average,
        notional=notional,
        fee=notional * fee_rate,
        fully_filled=remaining <= 0,
    )
