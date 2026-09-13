from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from dydx_v4_client.indexer.rest.indexer_client import IndexerClient

from ..config import ExchangeSettings
from ..valuation import order_book_midpoint
from .common import normalize_order_book


DYDX_MAINNET_INDEXER_URL = "https://indexer.dydx.trade"
LOGGER = logging.getLogger(__name__)
METADATA_TTL_SECONDS = 60 * 60
METADATA_STALE_MAX_SECONDS = 24 * 60 * 60
PRICE_TTL_SECONDS = 1.0


class DydxAdapter:
    exchange_id = "dydx"

    def __init__(self, config: ExchangeSettings):
        self.config = config
        self._runner = asyncio.Runner()
        self.indexer = IndexerClient(DYDX_MAINNET_INDEXER_URL)
        self._markets: dict[str, dict[str, Any]] = {}
        self._aliases = {symbol: symbol for symbol in getattr(config, "symbols", ())}
        self._metadata_cache: dict[str, dict[str, Any]] = {}
        self._metadata_loaded_at: float | None = None
        self._metadata_last_error: str | None = None
        self._price_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._io_lock = threading.RLock()

    def _run(self, coroutine):
        with self._io_lock:
            return self._runner.run(coroutine)

    def _request_market_raw(self, symbol: str) -> dict[str, Any]:
        response = self._run(self.indexer.markets.get_perpetual_markets(symbol))
        markets = response.get("markets") or {}
        raw = markets.get(symbol)
        if raw is None:
            raw = next((item for item in markets.values() if item.get("ticker") == symbol), None)
        if raw is None:
            raise RuntimeError(f"dYdX market not found: {symbol}")
        return dict(raw)

    def _build_aliases(self, markets: dict[str, dict[str, Any]]) -> dict[str, str]:
        aliases = {symbol: symbol for symbol in getattr(self.config, "symbols", ())}
        for symbol, raw in markets.items():
            for alias in {symbol, str(raw.get("ticker") or ""), str(raw.get("id") or "")}:
                if not alias:
                    continue
                previous = aliases.get(alias)
                if previous is not None and previous != symbol:
                    raise RuntimeError(f"market symbol alias is ambiguous: dydx {alias}")
                aliases[alias] = symbol
        return aliases

    def _market_raw(self, symbol: str, *, reload: bool = False) -> dict[str, Any]:
        markets = getattr(self, "_markets", {})
        if not reload and symbol in markets:
            return markets[symbol]
        raw = self._request_market_raw(symbol)
        with self._io_lock:
            updated = dict(self._markets)
            updated[symbol] = raw
            aliases = self._build_aliases(updated)
            self._markets = updated
            self._aliases = aliases
        return raw

    @property
    def metadata_age_seconds(self) -> float | None:
        if self._metadata_loaded_at is None:
            return None
        return max(0.0, time.monotonic() - self._metadata_loaded_at)

    @property
    def metadata_ready(self) -> bool:
        age = self.metadata_age_seconds
        return age is not None and age <= METADATA_STALE_MAX_SECONDS and bool(self._metadata_cache)

    @property
    def metadata_last_error(self) -> str | None:
        return self._metadata_last_error

    def resolve_symbol(self, raw_symbol: str) -> str:
        if raw_symbol != raw_symbol.strip():
            raise ValueError("symbol must not contain surrounding whitespace")
        aliases = getattr(self, "_aliases", None)
        if aliases is None:
            return raw_symbol if raw_symbol in getattr(self, "_markets", {}) else raw_symbol
        canonical = aliases.get(raw_symbol)
        if canonical is not None:
            return canonical
        self._refresh_instruments()
        canonical = self._aliases.get(raw_symbol)
        if canonical is None:
            raise ValueError(f"symbol is not configured for exchange: {raw_symbol}")
        return canonical

    def resolve_cached_symbol(self, raw_symbol: str) -> str | None:
        if raw_symbol != raw_symbol.strip():
            return None
        aliases = getattr(self, "_aliases", None)
        if aliases is None:
            return raw_symbol if raw_symbol in getattr(self, "_markets", {}) else None
        return aliases.get(raw_symbol)

    def fetch_order_book(self, symbol: str) -> dict[str, Any]:
        symbol = self.resolve_symbol(symbol)
        started = time.monotonic()
        response = self._run(self.indexer.markets.get_perpetual_market_orderbook(symbol))

        def levels(side: str) -> list[list[str]]:
            return [[str(item["price"]), str(item["size"])] for item in response.get(side, [])]

        raw = {
            "symbol": symbol,
            "timestamp": None,
            "nonce": None,
            "bids": levels("bids"),
            "asks": levels("asks"),
        }
        return normalize_order_book(
            raw,
            received_at=datetime.now(timezone.utc),
            request_duration_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _positive(value: Any) -> Decimal | None:
        if value is None or value == "":
            return None
        try:
            result = Decimal(str(value))
        except Exception:
            return None
        return result if result.is_finite() and result > 0 else None

    def _market_from_raw(self, symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
        step = Decimal(str(raw.get("stepSize") or (Decimal(10) ** int(raw.get("atomicResolution", -8)))))
        tick = Decimal(str(raw.get("tickSize") or "0.01"))
        min_qty = self._positive(raw.get("minOrderSize")) or step
        oracle_price = self._positive(raw.get("oraclePrice") or raw.get("indexPrice"))
        min_notional = self._positive(raw.get("minNotional") or raw.get("minOrderValue"))
        if min_notional is None and min_qty is not None and oracle_price is not None:
            min_notional = min_qty * oracle_price
        return {
            "id": symbol,
            "symbol": symbol,
            "spot": False,
            "swap": True,
            "base": symbol.split("-", 1)[0],
            "quote": "USD",
            "settle": "USDC",
            "precision": {"amount": str(step), "price": str(tick)},
            "limits": {"amount": {"min": min_qty}, "cost": {"min": min_notional}},
            "info": raw,
        }

    def market(self, symbol: str) -> dict[str, Any]:
        symbol = self.resolve_symbol(symbol)
        return self._market_from_raw(symbol, self._market_raw(symbol))

    def _instrument_from_raw(self, symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
        market = self._market_from_raw(symbol, raw)
        raw = market.get("info") or {}
        raw_status = str(raw.get("status") or "").lower()
        status = "active" if raw_status in {"active", "open", "trading"} else "inactive" if raw_status else "unknown"
        amount_limits = (market.get("limits") or {}).get("amount") or {}
        cost_limits = (market.get("limits") or {}).get("cost") or {}
        max_qty = Decimal(str(amount_limits.get("max"))) if amount_limits.get("max") is not None else None
        return {
            "exchange_id": self.exchange_id,
            "symbol": symbol,
            "base_asset": market.get("base"),
            "quote_asset": market.get("quote"),
            "settle_asset": market.get("settle"),
            "contract_type": "perpetual",
            "contract_size": Decimal("1"),
            "qty_step": Decimal(str((market.get("precision") or {}).get("amount"))),
            "min_qty": Decimal(str(amount_limits.get("min"))) if amount_limits.get("min") is not None else None,
            "max_qty": max_qty,
            "max_market_qty": max_qty,
            "price_step": Decimal(str((market.get("precision") or {}).get("price"))),
            "min_notional": Decimal(str(cost_limits.get("min"))) if cost_limits.get("min") is not None else None,
            "quantity_unit": market.get("base"),
            "status": status,
        }

    def _refresh_instruments(self) -> None:
        with self._io_lock:
            age = self.metadata_age_seconds
            if age is not None and age <= METADATA_TTL_SECONDS:
                return
            try:
                refreshed_markets = {
                    symbol: self._request_market_raw(symbol) for symbol in self.config.symbols
                }
                refreshed_aliases = self._build_aliases(refreshed_markets)
                refreshed = {
                    symbol: self._instrument_from_raw(symbol, refreshed_markets[symbol])
                    for symbol in self.config.symbols
                }
                self._markets = refreshed_markets
                self._aliases = refreshed_aliases
                self._metadata_cache = refreshed
                self._metadata_loaded_at = time.monotonic()
                self._metadata_last_error = None
            except Exception as exc:
                self._metadata_last_error = str(exc)
                age = self.metadata_age_seconds
                if age is None or age > METADATA_STALE_MAX_SECONDS:
                    raise
                LOGGER.warning(
                    "using stale instrument metadata: exchange=dydx cache_age_seconds=%.3f",
                    age,
                    exc_info=True,
                )

    def fetch_instruments(self, symbol: str | None = None) -> list[dict[str, Any]]:
        self._refresh_instruments()
        if symbol is not None:
            canonical = self.resolve_symbol(symbol)
            return [dict(self._metadata_cache[canonical])]
        return [dict(self._metadata_cache[item]) for item in self.config.symbols]

    def _fetch_price(self, symbol: str) -> dict[str, Any]:
        book = self.fetch_order_book(symbol)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        if not bids or not asks:
            raise RuntimeError(f"price has no usable order book: dydx {symbol}")
        bid = self._positive(bids[0][0])
        ask = self._positive(asks[0][0])
        if bid is None or ask is None or ask < bid:
            raise RuntimeError(f"price has an invalid order book: dydx {symbol}")
        mid_price, observed_at = order_book_midpoint(
            book,
            now=datetime.now(timezone.utc),
            max_age_seconds=self.config.market_data_max_age_seconds,
        )
        raw: dict[str, Any] = {}
        try:
            raw = self._market_raw(symbol, reload=True)
        except Exception:
            LOGGER.warning("dYdX ticker metadata lookup failed: symbol=%s", symbol, exc_info=True)
        mark = self._positive(raw.get("oraclePrice") or raw.get("indexPrice"))
        last = self._positive(raw.get("price") or raw.get("lastPrice"))
        return {
            "exchange_id": self.exchange_id,
            "symbol": symbol,
            "mark_price": mark,
            "last_price": last,
            "bid_price": bid,
            "ask_price": ask,
            "mid_price": mid_price,
            "observed_at": observed_at,
        }

    def fetch_prices(self, symbols: list[str] | tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        requested = list(symbols) if symbols is not None else list(self.config.symbols)
        with self._io_lock:
            result: list[dict[str, Any]] = []
            now = time.monotonic()
            for raw_symbol in requested:
                symbol = self.resolve_symbol(raw_symbol)
                cached = self._price_cache.get(symbol)
                if cached is not None and now - cached[0] <= PRICE_TTL_SECONDS:
                    result.append(dict(cached[1]))
                    continue
                price = self._fetch_price(symbol)
                self._price_cache[symbol] = (time.monotonic(), price)
                result.append(dict(price))
            return result

    def close(self) -> None:
        self._runner.close()
