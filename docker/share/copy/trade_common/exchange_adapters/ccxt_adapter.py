from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import ccxt

from ..config import ExchangeSettings
from ..valuation import order_book_midpoint
from .common import normalize_order_book


LOGGER = logging.getLogger(__name__)
METADATA_TTL_SECONDS = 60 * 60
METADATA_STALE_MAX_SECONDS = 24 * 60 * 60
PRICE_TTL_SECONDS = 1.0


def _decimal(value: Any, *, positive: bool = False) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite() or (positive and result <= 0):
        return None
    return result


class CcxtAdapter:
    def __init__(self, config: ExchangeSettings):
        exchange_class = getattr(ccxt, config.exchange_id, None)
        if exchange_class is None:
            raise RuntimeError(f"unsupported CCXT exchange: {config.exchange_id}")
        self.exchange_id = config.exchange_id
        self.config = config
        client_params: dict[str, Any] = {"enableRateLimit": True}
        options = dict(getattr(config, "options", {}) or {})
        if options:
            client_params["options"] = options
        self.client = exchange_class(client_params)
        self._lock = threading.RLock()
        self._alias_to_canonical: dict[str, str] = {}
        self._canonical_to_exchange: dict[str, str] = {}
        self._instrument_cache: dict[str, dict[str, Any]] = {}
        self._metadata_loaded_at: float | None = None
        self._metadata_last_error: str | None = None
        self._price_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        markets = self.client.load_markets()
        self._replace_markets(markets or getattr(self.client, "markets", {}) or {})

    @property
    def metadata_age_seconds(self) -> float | None:
        if self._metadata_loaded_at is None:
            return None
        return max(0.0, time.monotonic() - self._metadata_loaded_at)

    @property
    def metadata_ready(self) -> bool:
        age = self.metadata_age_seconds
        return age is not None and age <= METADATA_STALE_MAX_SECONDS and bool(self._instrument_cache)

    @property
    def metadata_last_error(self) -> str | None:
        return self._metadata_last_error

    def _precision_step(self, value: Any) -> Decimal | None:
        parsed = _decimal(value, positive=True)
        if parsed is None:
            return None
        mode = getattr(self.client, "precisionMode", None)
        decimal_places = getattr(ccxt, "DECIMAL_PLACES", 2)
        significant_digits = getattr(ccxt, "SIGNIFICANT_DIGITS", 3)
        if mode in {decimal_places, significant_digits} and parsed == parsed.to_integral_value():
            places = int(parsed)
            if places < 0 or places > 36:
                return None
            return Decimal(1).scaleb(-places)
        return parsed

    def _instrument(self, canonical: str, market: dict[str, Any]) -> dict[str, Any]:
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        precision = market.get("precision") or {}
        info = market.get("info") if isinstance(market.get("info"), dict) else {}
        lot_size = info.get("lotSizeFilter") if isinstance(info.get("lotSizeFilter"), dict) else {}
        active = market.get("active")
        if active is None:
            raw_status = str(info.get("status") or info.get("contractStatus") or "").lower()
            status = "active" if raw_status in {"active", "trading", "tradingstatus"} else "unknown"
        else:
            status = "active" if active else "inactive"
        contract_type = "unknown"
        if market.get("spot"):
            contract_type = "spot"
        elif market.get("swap"):
            contract_type = "linear_perpetual" if market.get("linear") else "inverse_perpetual" if market.get("inverse") else "perpetual"
        elif market.get("future"):
            contract_type = "linear_future" if market.get("linear") else "inverse_future" if market.get("inverse") else "future"

        min_notional = _decimal(cost_limits.get("min"), positive=True)
        if min_notional is None and lot_size.get("minNotionalValue") is not None:
            min_notional = _decimal(lot_size.get("minNotionalValue"), positive=True)

        max_market_qty = _decimal(lot_size.get("maxMktOrderQty"), positive=True)
        if max_market_qty is None:
            max_market_qty = _decimal(amount_limits.get("max"), positive=True)

        return {
            "exchange_id": self.exchange_id,
            "symbol": canonical,
            "base_asset": market.get("base"),
            "quote_asset": market.get("quote"),
            "settle_asset": market.get("settle") or market.get("quote"),
            "contract_type": contract_type,
            "contract_size": _decimal(market.get("contractSize"), positive=True),
            "qty_step": self._precision_step(precision.get("amount")),
            "min_qty": _decimal(amount_limits.get("min"), positive=True),
            "max_qty": _decimal(amount_limits.get("max"), positive=True),
            "max_market_qty": max_market_qty,
            "price_step": self._precision_step(precision.get("price")),
            "min_notional": min_notional,
            "quantity_unit": market.get("base"),
            "status": status,
        }

    def _replace_markets(self, markets: dict[str, dict[str, Any]]) -> None:
        configured = tuple(getattr(self.config, "symbols", ()) or ())
        if not configured:
            return
        alias_to_canonical: dict[str, str] = {}
        canonical_to_exchange: dict[str, str] = {}
        instruments: dict[str, dict[str, Any]] = {}
        market_owners: dict[str, str] = {}
        market_values = list(markets.values())
        for canonical in configured:
            matches = [
                market
                for market in market_values
                if canonical in {str(market.get("id") or ""), str(market.get("symbol") or "")}
            ]
            default_type = str((getattr(self.config, "options", {}) or {}).get("defaultType") or "").lower()
            if len(matches) > 1 and default_type in {"linear", "swap", "future"}:
                derivatives = [
                    market
                    for market in matches
                    if (market.get("swap") or market.get("future"))
                    and (default_type != "linear" or market.get("linear"))
                ]
                if derivatives:
                    matches = derivatives
            if len(matches) > 1 and default_type == "spot":
                spot_matches = [market for market in matches if market.get("spot")]
                if spot_matches:
                    matches = spot_matches
            if len(matches) != 1:
                reason = "not found" if not matches else "ambiguous"
                raise RuntimeError(f"configured market is {reason}: {self.exchange_id} {canonical}")
            market = matches[0]
            exchange_symbol = str(market.get("symbol") or market.get("id") or "")
            market_identity = str(market.get("id") or exchange_symbol)
            owner = market_owners.get(market_identity)
            if owner is not None and owner != canonical:
                raise RuntimeError(
                    f"configured symbols resolve to the same market: {self.exchange_id} {owner} {canonical}"
                )
            market_owners[market_identity] = canonical
            for alias in {canonical, str(market.get("id") or ""), str(market.get("symbol") or "")}:
                if not alias:
                    continue
                previous = alias_to_canonical.get(alias)
                if previous is not None and previous != canonical:
                    raise RuntimeError(f"market symbol alias is ambiguous: {self.exchange_id} {alias}")
                alias_to_canonical[alias] = canonical
            canonical_to_exchange[canonical] = exchange_symbol
            instruments[canonical] = self._instrument(canonical, market)
        with self._lock:
            self._alias_to_canonical = alias_to_canonical
            self._canonical_to_exchange = canonical_to_exchange
            self._instrument_cache = instruments
            self._metadata_loaded_at = time.monotonic()
            self._metadata_last_error = None

    def _refresh_metadata(self) -> None:
        with self._lock:
            age = self.metadata_age_seconds
            if age is not None and age <= METADATA_TTL_SECONDS:
                return
            try:
                try:
                    markets = self.client.load_markets(reload=True)
                except TypeError:
                    markets = self.client.load_markets(True)
                self._replace_markets(markets or getattr(self.client, "markets", {}) or {})
            except Exception as exc:
                self._metadata_last_error = str(exc)
                age = self.metadata_age_seconds
                if age is None or age > METADATA_STALE_MAX_SECONDS:
                    raise
                LOGGER.warning(
                    "using stale instrument metadata: exchange=%s cache_age_seconds=%.3f",
                    self.exchange_id,
                    age,
                    exc_info=True,
                )

    def resolve_symbol(self, raw_symbol: str) -> str:
        if raw_symbol != raw_symbol.strip():
            raise ValueError("symbol must not contain surrounding whitespace")
        self._refresh_metadata()
        canonical = self._alias_to_canonical.get(raw_symbol)
        if canonical is None:
            raise ValueError(f"symbol is not configured for exchange: {raw_symbol}")
        return canonical

    def resolve_cached_symbol(self, raw_symbol: str) -> str | None:
        if raw_symbol != raw_symbol.strip():
            return None
        with self._lock:
            return self._alias_to_canonical.get(raw_symbol)

    def _exchange_symbol(self, raw_symbol: str) -> tuple[str, str]:
        canonical = self.resolve_symbol(raw_symbol)
        return canonical, self._canonical_to_exchange[canonical]

    def fetch_instruments(self, symbol: str | None = None) -> list[dict[str, Any]]:
        self._refresh_metadata()
        if symbol is not None:
            canonical = self.resolve_symbol(symbol)
            return [dict(self._instrument_cache[canonical])]
        return [dict(self._instrument_cache[item]) for item in self.config.symbols]

    @staticmethod
    def _ticker_value(ticker: dict[str, Any], key: str, *info_keys: str) -> Decimal | None:
        value = _decimal(ticker.get(key), positive=True)
        if value is not None:
            return value
        info = ticker.get("info") or {}
        for info_key in info_keys:
            value = _decimal(info.get(info_key), positive=True)
            if value is not None:
                return value
        return None

    def _fetch_price(self, canonical: str, exchange_symbol: str) -> dict[str, Any]:
        ticker: dict[str, Any] = {}
        fetch_ticker = getattr(self.client, "fetch_ticker", None)
        if callable(fetch_ticker):
            try:
                ticker = fetch_ticker(exchange_symbol) or {}
            except Exception:
                LOGGER.warning(
                    "ticker lookup failed: exchange=%s symbol=%s",
                    self.exchange_id,
                    canonical,
                    exc_info=True,
                )
        book = self.fetch_order_book(canonical)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            raise RuntimeError(f"price has no usable order book: {self.exchange_id} {canonical}")
        bid = _decimal(bids[0][0], positive=True)
        ask = _decimal(asks[0][0], positive=True)
        if bid is None or ask is None or ask < bid:
            raise RuntimeError(f"price has an invalid order book: {self.exchange_id} {canonical}")
        mid_price, observed_at = order_book_midpoint(
            book,
            now=datetime.now(timezone.utc),
            max_age_seconds=self.config.market_data_max_age_seconds,
        )
        return {
            "exchange_id": self.exchange_id,
            "symbol": canonical,
            "mark_price": self._ticker_value(ticker, "mark", "markPrice", "mark_price"),
            "last_price": self._ticker_value(ticker, "last", "lastPrice", "last_price"),
            "bid_price": bid,
            "ask_price": ask,
            "mid_price": mid_price,
            "observed_at": observed_at,
        }

    def fetch_prices(self, symbols: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        requested = list(symbols) if symbols is not None else list(self.config.symbols)
        resolved = [self.resolve_symbol(symbol) for symbol in requested]
        with self._lock:
            now = time.monotonic()
            result: list[dict[str, Any]] = []
            for canonical in resolved:
                cached = self._price_cache.get(canonical)
                if cached is not None and now - cached[0] <= PRICE_TTL_SECONDS:
                    result.append(dict(cached[1]))
                    continue
                price = self._fetch_price(canonical, self._canonical_to_exchange[canonical])
                self._price_cache[canonical] = (time.monotonic(), price)
                result.append(dict(price))
            return result

    def fetch_order_book(self, symbol: str) -> dict[str, Any]:
        _, exchange_symbol = self._exchange_symbol(symbol)
        started = time.monotonic()
        raw = self.client.fetch_order_book(exchange_symbol)
        return normalize_order_book(
            raw,
            received_at=datetime.now(timezone.utc),
            request_duration_seconds=time.monotonic() - started,
        )

    def market(self, symbol: str) -> dict[str, Any]:
        _, exchange_symbol = self._exchange_symbol(symbol)
        return self.client.market(exchange_symbol)

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()
