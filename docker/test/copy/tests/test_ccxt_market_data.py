from decimal import Decimal

import pytest

from trade_common.config import ExchangeSettings
from trade_common.exchange_adapters.ccxt_adapter import CcxtAdapter
from trade_common.market_rules import MarketRuleError


def test_bybit_resolves_unconfigured_linear_market_from_catalog(monkeypatch):
    class FakeBybit:
        def __init__(self, _params):
            self.markets = {
                "ETH/USDT:USDT": {
                    "id": "ETHUSDT", "symbol": "ETH/USDT:USDT", "swap": True,
                    "linear": True, "active": True, "base": "ETH", "quote": "USDT",
                    "settle": "USDT", "precision": {"amount": 0.01},
                    "limits": {"amount": {"min": 0.01}},
                    "info": {"lotSizeFilter": {"maxMktOrderQty": "20"}},
                },
                "ADA/USDT": {
                    "id": "ADAUSDT", "symbol": "ADA/USDT", "spot": True,
                    "base": "ADA", "quote": "USDT",
                },
            }

        def load_markets(self, *args, **kwargs):
            return self.markets

    monkeypatch.setattr("trade_common.exchange_adapters.ccxt_adapter.ccxt.bybit", FakeBybit)
    adapter = CcxtAdapter(ExchangeSettings(
        exchange_id="bybit", adapter="ccxt", symbols=(),
        taker_fee_rate=Decimal("0.001"), maker_fee_rate=Decimal("0.001"),
        options={"defaultType": "linear"},
    ))

    assert adapter.metadata_ready
    assert adapter.fetch_instruments() == []
    assert adapter.resolve_symbol("ETH/USDT:USDT") == "ETHUSDT"
    assert adapter.fetch_instruments("ETHUSDT")[0]["max_market_qty"] == Decimal("20")
    with pytest.raises(MarketRuleError) as unsupported:
        adapter.resolve_symbol("ADAUSDT")
    assert unsupported.value.reason_code == "unsupported_market"
    with pytest.raises(MarketRuleError) as unknown:
        adapter.resolve_symbol("DOGEUSDT")
    assert unknown.value.reason_code == "unknown_symbol"


def test_empty_bybit_catalog_does_not_report_metadata_ready(monkeypatch):
    class EmptyBybit:
        def __init__(self, _params):
            self.markets = {}

        def load_markets(self, *args, **kwargs):
            return {}

    monkeypatch.setattr("trade_common.exchange_adapters.ccxt_adapter.ccxt.bybit", EmptyBybit)
    with pytest.raises(RuntimeError, match="catalog is empty"):
        CcxtAdapter(ExchangeSettings(
            exchange_id="bybit", adapter="ccxt", symbols=(),
            taker_fee_rate=Decimal("0.001"), maker_fee_rate=Decimal("0.001"),
            options={"defaultType": "linear"},
        ))


def test_bybit_options_select_linear_market_and_fetch_fresh_prices(monkeypatch):
    calls = {"books": 0}

    class FakeBybit:
        precisionMode = 4

        def __init__(self, params):
            assert params == {"enableRateLimit": True, "options": {"defaultType": "linear"}}
            self.markets = {
                "BTC/USDT": {
                    "id": "BTCUSDT",
                    "symbol": "BTC/USDT",
                    "spot": True,
                    "base": "BTC",
                    "quote": "USDT",
                    "precision": {"amount": 0.000001, "price": 0.01},
                    "limits": {"amount": {"min": 0.000001}, "cost": {"min": 1}},
                },
                "BTC/USDT:USDT": {
                    "id": "BTCUSDT",
                    "symbol": "BTC/USDT:USDT",
                    "swap": True,
                    "linear": True,
                    "active": True,
                    "base": "BTC",
                    "quote": "USDT",
                    "settle": "USDT",
                    "contractSize": 1,
                    "precision": {"amount": 0.001, "price": 0.1},
                    "limits": {"amount": {"min": 0.001, "max": 100}, "cost": {"min": 5}},
                },
            }

        def load_markets(self, *args, **kwargs):
            return self.markets

        def fetch_ticker(self, symbol):
            assert symbol == "BTC/USDT:USDT"
            return {"last": "100", "info": {"markPrice": "101"}}

        def fetch_order_book(self, symbol):
            assert symbol == "BTC/USDT:USDT"
            calls["books"] += 1
            return {"bids": [["99", "1"]], "asks": [["101", "1"]]}

        def market(self, symbol):
            return self.markets[symbol]

    monkeypatch.setattr("trade_common.exchange_adapters.ccxt_adapter.ccxt.bybit", FakeBybit)
    adapter = CcxtAdapter(
        ExchangeSettings(
            exchange_id="bybit",
            adapter="ccxt",
            symbols=("BTCUSDT",),
            taker_fee_rate=Decimal("0.001"),
            maker_fee_rate=Decimal("0.001"),
            options={"defaultType": "linear"},
        )
    )

    instrument = adapter.fetch_instruments("BTC/USDT:USDT")[0]
    first = adapter.fetch_prices(["BTCUSDT"])[0]
    second = adapter.fetch_prices(["BTC/USDT:USDT"])[0]

    assert adapter.resolve_symbol("BTC/USDT:USDT") == "BTCUSDT"
    assert instrument["contract_type"] == "linear_perpetual"
    assert instrument["qty_step"] == Decimal("0.001")
    assert instrument["min_notional"] == Decimal("5")
    assert instrument["quantity_unit"] == "BTC"
    assert instrument["max_market_qty"] is None
    assert instrument["max_qty"] == Decimal("100")
    assert first["mid_price"] == Decimal("100")
    assert first["mark_price"] == Decimal("101")
    assert second["mid_price"] == Decimal("100")
    assert calls["books"] == 2


def test_bybit_instrument_metadata_fallback_lot_size_filter(monkeypatch):
    class FakeBybit:
        precisionMode = 4

        def __init__(self, params):
            self.markets = {
                "BTC/USDT:USDT": {
                    "id": "BTCUSDT",
                    "symbol": "BTC/USDT:USDT",
                    "swap": True,
                    "linear": True,
                    "active": True,
                    "base": "BTC",
                    "quote": "USDT",
                    "settle": "USDT",
                    "contractSize": 1,
                    "precision": {"amount": 0.001, "price": 0.1},
                    "limits": {"amount": {"min": 0.001, "max": 1500.0}, "cost": {"min": None}},
                    "info": {
                        "lotSizeFilter": {
                            "minNotionalValue": "5",
                            "maxMktOrderQty": "150.000",
                            "maxOrderQty": "1500.000",
                            "minOrderQty": "0.001",
                        }
                    },
                },
            }

        def load_markets(self, *args, **kwargs):
            return self.markets

        def market(self, symbol):
            return self.markets[symbol]

    monkeypatch.setattr("trade_common.exchange_adapters.ccxt_adapter.ccxt.bybit", FakeBybit)
    adapter = CcxtAdapter(
        ExchangeSettings(
            exchange_id="bybit",
            adapter="ccxt",
            symbols=("BTCUSDT",),
            taker_fee_rate=Decimal("0.001"),
            maker_fee_rate=Decimal("0.001"),
            options={"defaultType": "linear"},
        )
    )

    instrument = adapter.fetch_instruments("BTC/USDT:USDT")[0]
    assert instrument["min_notional"] == Decimal("5")
    assert instrument["max_qty"] == Decimal("1500.0")
    assert instrument["max_market_qty"] == Decimal("150.0")
    assert instrument["quantity_unit"] == "BTC"
