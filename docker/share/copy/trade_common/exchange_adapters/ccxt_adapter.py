from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import ccxt

from ..config import ExchangeSettings
from ..market_rules import BYBIT_NATIVE_SYMBOL, MarketRuleError, bybit_local_canonical, positive_decimal
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
        self._unsupported_aliases: set[str] = set()
        self._inconclusive_aliases: set[str] = set()
        self._markets_loaded = False
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
        ttl = self.config.metadata_ttl_seconds if self.exchange_id == "bybit" else METADATA_STALE_MAX_SECONDS
        return age is not None and age <= ttl and self._markets_loaded

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

        invalid_fields: list[str] = []

        def parsed(raw: Any, field: str) -> Decimal | None:
            value = _decimal(raw, positive=True)
            if raw is not None and value is None:
                invalid_fields.append(field)
            return value

        min_notional_raw = cost_limits.get("min")
        if min_notional_raw is None:
            min_notional_raw = lot_size.get("minNotionalValue")
        min_notional = parsed(min_notional_raw, "min_notional")
        max_market_qty = parsed(lot_size.get("maxMktOrderQty"), "max_market_qty")
        min_qty_raw = amount_limits.get("min")
        if min_qty_raw is None:
            min_qty_raw = lot_size.get("minOrderQty")
        max_qty_raw = amount_limits.get("max")
        if max_qty_raw is None:
            max_qty_raw = lot_size.get("maxOrderQty")
        qty_step_raw = precision.get("amount")
        if qty_step_raw is None:
            qty_step_raw = lot_size.get("qtyStep")
        price_filter = info.get("priceFilter") if isinstance(info.get("priceFilter"), dict) else {}
        price_step_raw = price_filter.get("tickSize")
        if price_step_raw is None:
            price_step_raw = precision.get("price")
        # Bybit's tickSize is already a step, while CCXT precision may encode
        # decimal places depending on precisionMode.
        price_step = (
            _decimal(price_step_raw, positive=True)
            if price_filter.get("tickSize") is not None
            else self._precision_step(price_step_raw)
        )
        if price_step_raw is not None and price_step is None:
            invalid_fields.append("price_step")

        return {
            "exchange_id": self.exchange_id,
            "symbol": canonical,
            "base_asset": market.get("base"),
            "quote_asset": market.get("quote"),
            "settle_asset": market.get("settle") if self.exchange_id == "bybit" else market.get("settle") or market.get("quote"),
            "contract_type": contract_type,
            "contract_size": _decimal(market.get("contractSize"), positive=True),
            "qty_step": self._precision_step(qty_step_raw),
            "min_qty": parsed(min_qty_raw, "min_qty"),
            "max_qty": parsed(max_qty_raw, "max_qty"),
            "max_market_qty": max_market_qty,
            "price_step": price_step,
            "min_notional": min_notional,
            "quantity_unit": market.get("base"),
            "status": status,
            "_invalid_fields": invalid_fields,
        }

    def _replace_markets(self, markets: dict[str, dict[str, Any]]) -> None:
        if self.exchange_id == "bybit":
            if not markets:
                raise RuntimeError("Bybit market catalog is empty")
            aliases: dict[str, str] = {}
            exchange_symbols: dict[str, str] = {}
            instruments: dict[str, dict[str, Any]] = {}
            known: set[str] = set()
            inconclusive: set[str] = set()
            for market in markets.values():
                if not isinstance(market, dict):
                    continue
                market_aliases = {str(market.get("id") or ""), str(market.get("symbol") or "")}
                known.update(alias for alias in market_aliases if alias)
                explicit_unsupported = (
                    market.get("spot") is True
                    or market.get("future") is True
                    or market.get("inverse") is True
                    or market.get("swap") is False
                    or market.get("linear") is False
                    or market.get("quote") not in {None, "USDT"}
                    or market.get("settle") not in {None, "USDT"}
                )
                if not explicit_unsupported and any(
                    market.get(field) is None for field in ("swap", "linear", "quote", "settle")
                ):
                    inconclusive.update(alias for alias in market_aliases if alias)
                if not (
                    market.get("swap") is True
                    and market.get("linear") is True
                    and market.get("quote") == "USDT"
                    and market.get("settle") == "USDT"
                ):
                    continue
                canonical = str(market.get("id") or "")
                exchange_symbol = str(market.get("symbol") or "")
                if not exchange_symbol or BYBIT_NATIVE_SYMBOL.fullmatch(canonical) is None:
                    continue
                if canonical in exchange_symbols and exchange_symbols[canonical] != exchange_symbol:
                    raise RuntimeError(f"ambiguous Bybit market id: {canonical}")
                exchange_symbols[canonical] = exchange_symbol
                instruments[canonical] = self._instrument(canonical, market)
                for alias in market_aliases:
                    if alias:
                        previous = aliases.get(alias)
                        if previous is not None and previous != canonical:
                            raise RuntimeError(f"ambiguous Bybit market alias: {alias}")
                        aliases[alias] = canonical
            with self._lock:
                self._alias_to_canonical = aliases
                self._canonical_to_exchange = exchange_symbols
                self._instrument_cache = instruments
                self._unsupported_aliases = known - set(aliases) - inconclusive
                self._inconclusive_aliases = inconclusive - set(aliases)
                self._metadata_loaded_at = time.monotonic()
                self._metadata_last_error = None
                self._markets_loaded = True
            return
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
            self._markets_loaded = True

    def _refresh_metadata(self) -> None:
        with self._lock:
            age = self.metadata_age_seconds
            ttl = self.config.metadata_ttl_seconds if self.exchange_id == "bybit" else METADATA_TTL_SECONDS
            if age is not None and age <= ttl:
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
                stale_limit = ttl if self.exchange_id == "bybit" else METADATA_STALE_MAX_SECONDS
                if age is None or age > stale_limit:
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
        if self.exchange_id == "bybit" and bybit_local_canonical(raw_symbol) is None:
            raise MarketRuleError("unknown_symbol", f"unknown Bybit symbol: {raw_symbol}", 422)
        self._refresh_metadata()
        canonical = self._alias_to_canonical.get(raw_symbol)
        if canonical is None:
            if self.exchange_id == "bybit":
                if raw_symbol in self._inconclusive_aliases:
                    raise MarketRuleError("instrument_metadata_invalid", f"Bybit market metadata is incomplete: {raw_symbol}", 503)
                if raw_symbol in self._unsupported_aliases:
                    raise MarketRuleError("unsupported_market", f"unsupported Bybit market: {raw_symbol}", 422)
                raise MarketRuleError("unknown_symbol", f"unknown Bybit symbol: {raw_symbol}", 422)
            raise ValueError(f"symbol is not configured for exchange: {raw_symbol}")
        return canonical

    def refresh_metadata(self) -> None:
        self._refresh_metadata()

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
        age = self.metadata_age_seconds
        stale = self.exchange_id != "bybit" and age is not None and age > METADATA_TTL_SECONDS
        def view(canonical: str) -> dict[str, Any]:
            return {
                **self._instrument_cache[canonical],
                "metadata_age_seconds": age,
                "metadata_stale": stale,
            }
        if symbol is not None:
            canonical = self.resolve_symbol(symbol)
            return [view(canonical)]
        return [view(self.resolve_symbol(item)) for item in self.config.symbols]

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
        mark_price = None
        mark_observed_at = None
        if self.exchange_id == "bybit":
            mark_price, mark_observed_at, ticker = self._fetch_mark_ticker(canonical, exchange_symbol)
        else:
            fetch_ticker = getattr(self.client, "fetch_ticker", None)
            if callable(fetch_ticker):
                try:
                    ticker = fetch_ticker(exchange_symbol) or {}
                except Exception:
                    LOGGER.warning("ticker lookup failed: exchange=%s symbol=%s", self.exchange_id, canonical, exc_info=True)
        try:
            book = self.fetch_order_book(canonical)
        except Exception:
            if self.exchange_id != "bybit":
                raise
            book = {}
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            if self.exchange_id != "bybit":
                raise RuntimeError(f"price has no usable order book: {self.exchange_id} {canonical}")
            bid = ask = mid_price = observed_at = None
        else:
            try:
                bid = _decimal(bids[0][0], positive=True)
                ask = _decimal(asks[0][0], positive=True)
                if bid is None or ask is None or ask < bid:
                    raise ValueError("invalid order book")
                mid_price, observed_at = order_book_midpoint(
                    book, now=datetime.now(timezone.utc),
                    max_age_seconds=self.config.market_data_max_age_seconds,
                )
            except Exception:
                if self.exchange_id != "bybit":
                    raise
                bid = ask = mid_price = observed_at = None
        return {
            "exchange_id": self.exchange_id,
            "symbol": canonical,
            "mark_price": mark_price if self.exchange_id == "bybit" else self._ticker_value(ticker, "mark", "markPrice", "mark_price"),
            "mark_observed_at": mark_observed_at,
            "last_price": self._ticker_value(ticker, "last", "lastPrice", "last_price"),
            "bid_price": bid,
            "ask_price": ask,
            "mid_price": mid_price,
            "observed_at": observed_at,
        }

    def _fetch_mark_ticker(self, canonical: str, exchange_symbol: str) -> tuple[Decimal, datetime, dict[str, Any]]:
        try:
            ticker = self.client.fetch_ticker(exchange_symbol) or {}
            mark = positive_decimal(self._ticker_value(ticker, "mark", "markPrice", "mark_price"))
            if mark is None:
                raise ValueError("Mark Price is missing or invalid")
            return mark, datetime.now(timezone.utc), ticker
        except Exception as exc:
            raise MarketRuleError("mark_price_unavailable", f"fresh Mark Price is unavailable for {canonical}", 503) from exc

    def fetch_mark_price(self, symbol: str) -> tuple[Decimal, datetime]:
        canonical, exchange_symbol = self._exchange_symbol(symbol)
        mark, observed_at, _ = self._fetch_mark_ticker(canonical, exchange_symbol)
        return mark, observed_at

    def fetch_prices(self, symbols: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        requested = list(symbols) if symbols is not None else list(self.config.symbols)
        resolved = [self.resolve_symbol(symbol) for symbol in requested]
        with self._lock:
            now = time.monotonic()
            result: list[dict[str, Any]] = []
            for canonical in resolved:
                cached = self._price_cache.get(canonical)
                if self.exchange_id != "bybit" and cached is not None and now - cached[0] <= PRICE_TTL_SECONDS:
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
