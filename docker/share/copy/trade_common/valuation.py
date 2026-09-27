from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import math
from typing import Any, Literal

from sqlalchemy import func, literal, select
from sqlalchemy.orm import Session

from .config import Settings
from .market_rules import fresh_mark_price, positive_decimal
from .models import DailyPnl, Fill, Position


class ValuationError(ValueError):
    pass


@dataclass(frozen=True)
class AccountBalance:
    currency: str
    initial_balance: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_fee: Decimal
    equity: Decimal
    used_margin: Decimal
    available_balance: Decimal
    updated_at: datetime


def position_side(quantity: Decimal) -> Literal["buy", "sell"]:
    if quantity > 0:
        return "buy"
    if quantity < 0:
        return "sell"
    raise ValuationError("a flat position has no side")


def market_assets(market: dict[str, Any] | None, symbol: str) -> tuple[str, str]:
    market = market or {}
    base = str(market.get("base") or "").strip()
    quote = str(market.get("quote") or "").strip()
    if base and quote:
        return base, quote

    separator = "/" if "/" in symbol else "-" if "-" in symbol else None
    if separator:
        fallback_base, fallback_quote = symbol.split(separator, 1)
        base = base or fallback_base
        quote = quote or fallback_quote.split(":", 1)[0]
    return base or symbol, quote


def order_book_midpoint(
    book: dict[str, Any],
    *,
    now: datetime,
    max_age_seconds: float,
) -> tuple[Decimal, datetime]:
    if not isinstance(book, dict):
        raise ValuationError("order book is invalid")
    try:
        duration = float(book.get("_request_duration_seconds") or 0)
    except (TypeError, ValueError) as exc:
        raise ValuationError("market data request duration is invalid") from exc
    if not math.isfinite(duration) or duration < 0:
        raise ValuationError("market data request duration is invalid")
    if duration > max_age_seconds:
        raise ValuationError("market data request was too slow")

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not isinstance(bids, (list, tuple)) or not isinstance(asks, (list, tuple)):
        raise ValuationError("order book levels are invalid")
    if not bids or not asks:
        raise ValuationError("order book is empty")
    for side, levels in (("bids", bids), ("asks", asks)):
        previous = None
        for level in levels:
            if not isinstance(level, (list, tuple)) or len(level) < 2:
                raise ValuationError("order book has an invalid level")
            try:
                price = Decimal(str(level[0]))
                quantity = Decimal(str(level[1]))
            except (IndexError, InvalidOperation, TypeError, ValueError) as exc:
                raise ValuationError("order book has an invalid level") from exc
            if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0:
                raise ValuationError("order book has an invalid level")
            if previous is not None and ((side == "bids" and price > previous) or (side == "asks" and price < previous)):
                raise ValuationError("order book levels are not in price order")
            previous = price
    best_bid = Decimal(str(bids[0][0]))
    best_ask = Decimal(str(asks[0][0]))
    if best_ask < best_bid:
        raise ValuationError("order book is crossed")

    timestamp_ms = book.get("timestamp")
    if timestamp_ms:
        try:
            observed_at = datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=timezone.utc)
        except (OSError, TypeError, ValueError) as exc:
            raise ValuationError("market data has an invalid timestamp") from exc
    else:
        observed_at = book.get("_received_at")
        if isinstance(observed_at, str):
            try:
                observed_at = datetime.fromisoformat(observed_at)
            except ValueError as exc:
                raise ValuationError("market data has an invalid timestamp") from exc
        if not isinstance(observed_at, datetime):
            raise ValuationError("market data has no timestamp")
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)

    age_seconds = (now - observed_at).total_seconds()
    if age_seconds < -2:
        raise ValuationError("market data timestamp is too far in the future")
    if age_seconds > max_age_seconds:
        raise ValuationError("market data is stale")
    return (best_bid + best_ask) / Decimal("2"), observed_at


def unrealized_pnl(quantity: Decimal, average_entry_price: Decimal, current_price: Decimal) -> Decimal:
    if quantity == 0:
        return Decimal("0")
    if average_entry_price <= 0:
        raise ValuationError("position has no valid average entry price")
    return quantity * (current_price - average_entry_price)


def calculate_account_balance(session: Session, config: Settings, adapter_pool) -> AccountBalance:
    if config.account.currency not in {"USD", "USDC", "USDT"}:
        raise ValuationError(f"unsupported account currency: {config.account.currency}")
    # One SQL statement gives all ledger components the same committed snapshot
    # under PostgreSQL READ COMMITTED. The later public market-data requests
    # hold no row locks on these ledger tables.
    anchor = select(literal(1).label("anchor")).subquery()
    realized_total = select(func.coalesce(func.sum(DailyPnl.realized_pnl), 0)).scalar_subquery()
    fee_total = select(func.coalesce(func.sum(Fill.fee), 0)).scalar_subquery()
    rows = session.execute(
        select(Position, realized_total, fee_total)
        .select_from(anchor)
        .outerjoin(Position, Position.quantity != 0)
    ).all()
    positions = [
        (item.exchange_id, item.symbol, Decimal(item.quantity), Decimal(item.average_entry_price))
        for item, _, _ in rows if item is not None
    ]
    realized = Decimal(rows[0][1] or 0)
    total_fee = Decimal(rows[0][2] or 0)
    total_unrealized = Decimal("0")
    used_margin = Decimal("0")
    allowed_stablecoins = {"USD", "USDC", "USDT"}

    grouped: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
    for exchange_id, symbol, quantity, entry in positions:
        grouped.setdefault(exchange_id, []).append((symbol, quantity, entry))

    for exchange_id, exchange_positions in grouped.items():
        try:
            adapter = adapter_pool.get(exchange_id)
        except Exception as exc:
            raise ValuationError(f"market data is unavailable for {exchange_id}") from exc
        for symbol, quantity, entry in exchange_positions:
            try:
                instrument = adapter.fetch_instruments(symbol)[0]
            except Exception as exc:
                raise ValuationError(f"instrument metadata is unavailable for {exchange_id} {symbol}") from exc
            if instrument.get("metadata_stale"):
                raise ValuationError(f"instrument metadata is stale for {exchange_id} {symbol}")
            settle_asset = str(
                instrument.get("settle_asset")
                or (instrument.get("quote_asset") if exchange_id != "bybit" else None)
                or ""
            )
            if settle_asset not in allowed_stablecoins:
                raise ValuationError(
                    f"unsupported settlement currency for {exchange_id} {symbol}: {settle_asset or 'unknown'}"
                )
            try:
                if exchange_id == "bybit":
                    price, _ = fresh_mark_price(adapter, symbol)
                else:
                    price_item = adapter.fetch_prices([symbol])[0]
                    price = positive_decimal(price_item.get("mid_price"))
            except Exception as exc:
                raise ValuationError(f"market price is unavailable for {exchange_id} {symbol}") from exc
            if price is None or not price.is_finite() or price <= 0:
                raise ValuationError(f"market price is unavailable for {exchange_id} {symbol}")
            total_unrealized += unrealized_pnl(quantity, entry, price)
            used_margin += abs(quantity) * price / config.account.default_leverage

    equity = config.account.initial_balance + realized + total_unrealized
    return AccountBalance(
        currency=config.account.currency,
        initial_balance=config.account.initial_balance,
        realized_pnl=realized,
        unrealized_pnl=total_unrealized,
        total_fee=total_fee,
        equity=equity,
        used_margin=used_margin,
        available_balance=max(Decimal("0"), equity - used_margin),
        updated_at=datetime.now(timezone.utc),
    )
