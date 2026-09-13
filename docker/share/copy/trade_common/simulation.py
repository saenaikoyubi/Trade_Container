from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable


@dataclass(frozen=True)
class SimulatedExecution:
    quantity: Decimal
    average_price: Decimal
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

    for raw_price, raw_quantity, *_ in levels:
        price = Decimal(str(raw_price))
        available = Decimal(str(raw_quantity))
        if available <= 0:
            continue
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
        fee=notional * fee_rate,
        fully_filled=remaining <= 0,
    )
