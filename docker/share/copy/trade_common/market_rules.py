from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


BYBIT_UNIFIED_SYMBOL = re.compile(r"^([A-Z0-9]+)/USDT:USDT$")
BYBIT_NATIVE_SYMBOL = re.compile(r"^[A-Z0-9]+USDT$")
TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "rejected", "failed"})
ACTIVE_CLOSE_STATUSES = frozenset({"queued", "running", "waiting", "canceling"})


class MarketRuleError(Exception):
    def __init__(self, reason_code: str, detail: str, status_code: int):
        super().__init__(detail)
        self.reason_code = reason_code
        self.detail = detail
        self.status_code = status_code


def bybit_local_canonical(symbol: str) -> str | None:
    match = BYBIT_UNIFIED_SYMBOL.fullmatch(symbol)
    if match:
        return f"{match.group(1)}USDT"
    return symbol if BYBIT_NATIVE_SYMBOL.fullmatch(symbol) else None


def bybit_history_symbols(symbol: str) -> tuple[str, ...]:
    canonical = bybit_local_canonical(symbol)
    return (symbol, canonical) if canonical is not None and canonical != symbol else (symbol,)


def positive_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def required_positive(value: Any, name: str, symbol: str) -> Decimal:
    parsed = positive_decimal(value)
    if parsed is None:
        raise MarketRuleError("instrument_metadata_invalid", f"{name} is missing or invalid for {symbol}", 503)
    return parsed


def fresh_mark_price(adapter, symbol: str) -> tuple[Decimal, datetime]:
    try:
        fetch = getattr(adapter, "fetch_mark_price", None)
        if callable(fetch):
            price, observed_at = fetch(symbol)
        else:
            item = adapter.fetch_prices([symbol])[0]
            price, observed_at = item.get("mark_price"), item.get("mark_observed_at")
        parsed = positive_decimal(price)
        if parsed is None or not isinstance(observed_at, datetime):
            raise ValueError("invalid Mark Price response")
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)
        return parsed, observed_at.astimezone(timezone.utc)
    except MarketRuleError:
        raise
    except Exception as exc:
        raise MarketRuleError("mark_price_unavailable", f"fresh Mark Price is unavailable for {symbol}", 503) from exc
