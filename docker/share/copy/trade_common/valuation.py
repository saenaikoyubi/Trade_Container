from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
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
    duration = float(book.get("_request_duration_seconds") or 0)
    if duration > max_age_seconds:
        raise ValuationError("market data request was too slow")

    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        raise ValuationError("order book is empty")
    try:
        best_bid = Decimal(str(bids[0][0]))
        best_ask = Decimal(str(asks[0][0]))
    except (IndexError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValuationError("order book has an invalid price") from exc
    if not best_bid.is_finite() or not best_ask.is_finite() or best_bid <= 0 or best_ask <= 0:
        raise ValuationError("order book has an invalid price")
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

    if (now - observed_at).total_seconds() > max_age_seconds:
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
    positions = list(session.scalars(select(Position).where(Position.quantity != 0)).all())
    realized = Decimal(
        session.scalar(select(func.coalesce(func.sum(DailyPnl.realized_pnl), 0))) or 0
    )
    total_fee = Decimal(session.scalar(select(func.coalesce(func.sum(Fill.fee), 0))) or 0)
    total_unrealized = Decimal("0")
    used_margin = Decimal("0")
    allowed_stablecoins = {"USD", "USDC", "USDT"}

    grouped: dict[str, list[Position]] = {}
    for position in positions:
        grouped.setdefault(position.exchange_id, []).append(position)

    for exchange_id, exchange_positions in grouped.items():
        try:
            adapter = adapter_pool.get(exchange_id)
            symbols = [position.symbol for position in exchange_positions]
            prices = adapter.fetch_prices(symbols)
            instruments = adapter.fetch_instruments()
        except Exception as exc:
            raise ValuationError(f"market data is unavailable for {exchange_id}") from exc
        price_by_symbol = {item["symbol"]: item for item in prices}
        instrument_by_symbol = {item["symbol"]: item for item in instruments}
        for position in exchange_positions:
            instrument = instrument_by_symbol.get(position.symbol)
            settle_asset = str(
                (instrument or {}).get("settle_asset")
                or (instrument or {}).get("quote_asset")
                or ""
            )
            if settle_asset not in allowed_stablecoins:
                raise ValuationError(
                    f"unsupported settlement currency for {exchange_id} {position.symbol}: {settle_asset or 'unknown'}"
                )
            price_item = price_by_symbol.get(position.symbol)
            price = Decimal(price_item["mid_price"]) if price_item and price_item.get("mid_price") is not None else None
            if price is None or not price.is_finite() or price <= 0:
                raise ValuationError(f"market price is unavailable for {exchange_id} {position.symbol}")
            quantity = Decimal(position.quantity)
            entry = Decimal(position.average_entry_price)
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
