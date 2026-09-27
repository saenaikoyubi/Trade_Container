"""Opt-in, read-only smoke checks against public exchange endpoints.

Run in a container with outbound network access and
RUN_LIVE_PUBLIC_MARKET_DATA=1. No account credentials or order APIs are used.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from trade_common.config import ExchangeSettings
from trade_common.exchange_adapters import create_exchange_adapter
from trade_common.risk import instrument_preflight_reason
from trade_common.valuation import order_book_midpoint


@pytest.mark.skipif(
    os.getenv("RUN_LIVE_PUBLIC_MARKET_DATA") != "1",
    reason="opt-in public exchange smoke test",
)
@pytest.mark.parametrize(
    ("exchange_id", "adapter_kind", "symbol", "options"),
    [
        ("bybit", "ccxt", "BTCUSDT", {"defaultType": "linear"}),
        ("binance", "ccxt", "BTC/USDT", {}),
        ("dydx", "dydx", "BTC-USD", {}),
    ],
)
def test_live_public_metadata_book_and_price(exchange_id, adapter_kind, symbol, options):
    config = ExchangeSettings(
        exchange_id=exchange_id, adapter=adapter_kind, symbols=(symbol,),
        taker_fee_rate=Decimal("0"), maker_fee_rate=Decimal("0"), options=options,
        market_data_max_age_seconds=60,
    )
    adapter = create_exchange_adapter(config)
    try:
        instrument = adapter.fetch_instruments(symbol)[0]
        assert instrument["symbol"] == symbol
        assert instrument["metadata_stale"] is False
        assert instrument["metadata_age_seconds"] is not None
        book = adapter.fetch_order_book(symbol)
        midpoint, _observed_at = order_book_midpoint(
            book, now=datetime.now(timezone.utc), max_age_seconds=60,
        )
        assert midpoint > 0
        price = adapter.fetch_prices([symbol])[0]
        assert price["symbol"] == symbol
        if exchange_id == "bybit":
            assert price["mark_price"] > 0
        else:
            assert price["mid_price"] > 0
        print({
            "exchange": exchange_id,
            "symbol": symbol,
            "status": instrument["status"],
            "min_qty": str(instrument.get("min_qty")),
            "qty_step": str(instrument.get("qty_step")),
            "price_step": str(instrument.get("price_step")),
            "min_notional": str(instrument.get("min_notional")),
            "market_reason": instrument_preflight_reason(instrument, "market"),
            "limit_reason": instrument_preflight_reason(instrument, "limit"),
        })
    finally:
        adapter.close()
