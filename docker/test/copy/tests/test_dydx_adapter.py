from decimal import Decimal
from threading import RLock

from trade_common.config import ExchangeSettings
from trade_common.exchange_adapters.dydx_adapter import DYDX_MAINNET_INDEXER_URL, DydxAdapter


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


def test_dydx_metadata_refreshes_from_network_and_derives_min_notional(monkeypatch):
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

    assert first["min_notional"] == Decimal("0.100")
    assert first["quantity_unit"] == "BTC"
    assert second["min_notional"] == Decimal("0.200")
    assert second["quantity_unit"] == "BTC"
    assert adapter._markets["BTC-USD"]["oraclePrice"] == "200"
