from decimal import Decimal
from threading import RLock
import time

import pytest

from trade_common.config import ExchangeSettings
from trade_common.exchange_adapters.dydx_adapter import DYDX_MAINNET_INDEXER_URL, DydxAdapter
from trade_common.risk import instrument_preflight_reason, validate_instrument_quantity, validate_limit_price


def test_dydx_adapter_uses_mainnet_indexer(monkeypatch):
    created_with = []

    class FakeIndexerClient:
        def __init__(self, url):
            created_with.append(url)

    monkeypatch.setattr("trade_common.exchange_adapters.dydx_adapter.IndexerClient", FakeIndexerClient)

    adapter = DydxAdapter(object())
    adapter.close()

    assert DYDX_MAINNET_INDEXER_URL == "https://indexer.dydx.trade"
    assert created_with == [DYDX_MAINNET_INDEXER_URL]


def test_dydx_market_metadata_is_normalized():
    adapter = object.__new__(DydxAdapter)
    adapter._markets = {
        "BTC-USD": {
            "ticker": "BTC-USD",
            "stepSize": "0.0001",
            "tickSize": "1",
        }
    }

    market = adapter.market("BTC-USD")

    assert market["base"] == "BTC"
    assert market["quote"] == "USD"
    assert market["precision"] == {"amount": "0.0001", "price": "1"}


def test_dydx_adapter_has_no_authenticated_trading_surface():
    for name in ("create_order", "fetch_order", "cancel_order", "fetch_balance", "fetch_positions", "fetch_fills"):
        assert not hasattr(DydxAdapter, name)


def test_dydx_metadata_refreshes_without_inventing_limits(monkeypatch):
    adapter = object.__new__(DydxAdapter)
    adapter.config = ExchangeSettings(
        exchange_id="dydx",
        adapter="dydx",
        symbols=("BTC-USD",),
        taker_fee_rate=Decimal("0.001"),
        maker_fee_rate=Decimal("0.001"),
    )
    adapter._io_lock = RLock()
    adapter._markets = {}
    adapter._aliases = {"BTC-USD": "BTC-USD"}
    adapter._metadata_cache = {}
    adapter._metadata_loaded_at = None
    adapter._metadata_last_error = None
    responses = iter(
        [
            {"ticker": "BTC-USD", "status": "ACTIVE", "stepSize": "0.001", "tickSize": "1", "oraclePrice": "100"},
            {"ticker": "BTC-USD", "status": "ACTIVE", "stepSize": "0.001", "tickSize": "1", "oraclePrice": "200"},
        ]
    )
    monkeypatch.setattr(adapter, "_request_market_raw", lambda _symbol: next(responses))

    first = adapter.fetch_instruments()[0]
    adapter._metadata_loaded_at = None
    second = adapter.fetch_instruments()[0]

    assert first["min_notional"] is None
    assert first["min_qty"] is None
    assert first["quantity_unit"] == "BTC"
    assert second["min_notional"] is None
    assert second["min_qty"] is None
    assert second["quantity_unit"] == "BTC"
    assert adapter._markets["BTC-USD"]["oraclePrice"] == "200"
    assert instrument_preflight_reason(second, "market") == "instrument_metadata_invalid"
    assert not validate_instrument_quantity(
        Decimal("0.001"), Decimal("200"), second, order_type="market",
    ).allowed
    adapter._metadata_loaded_at = time.monotonic() - 3601
    monkeypatch.setattr(adapter, "_request_market_raw", lambda _symbol: (_ for _ in ()).throw(RuntimeError("indexer unavailable")))
    assert adapter.fetch_instruments()[0]["metadata_stale"] is True
    adapter._metadata_loaded_at = time.monotonic() - (24 * 3600 + 1)
    with pytest.raises(RuntimeError, match="indexer unavailable"):
        adapter.fetch_instruments()


def test_dydx_missing_tick_is_not_substituted():
    adapter = object.__new__(DydxAdapter)
    instrument = adapter._instrument_from_raw("BTC-USD", {
        "stepSize": "0.001", "minOrderSize": "0.001", "status": "ACTIVE",
    })
    assert instrument["price_step"] is None
    assert instrument["qty_step"] == Decimal("0.001")
    assert instrument["min_notional"] is None
    assert instrument_preflight_reason(instrument, "market") is None
    assert instrument_preflight_reason(instrument, "limit") == "instrument_metadata_invalid"
    assert validate_limit_price(Decimal("100"), instrument, exchange_id="dydx").temporary
