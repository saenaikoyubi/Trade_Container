from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from trade_common.exchange import normalize_order_book
from trade_common.exchange_adapters.ccxt_adapter import CcxtAdapter


def test_order_book_identity_ignores_local_receive_time():
    raw = {"timestamp": None, "nonce": None, "bids": [["99", "1"]], "asks": [["100", "1"]]}
    first = normalize_order_book(
        raw,
        received_at=datetime(2026, 7, 14, tzinfo=timezone.utc),
        request_duration_seconds=0.1,
    )
    second = normalize_order_book(
        raw,
        received_at=datetime(2026, 7, 14, tzinfo=timezone.utc) + timedelta(seconds=1),
        request_duration_seconds=0.2,
    )

    assert first["_market_data_id"] == second["_market_data_id"]


def test_order_book_identity_changes_with_book_content():
    received_at = datetime(2026, 7, 14, tzinfo=timezone.utc)
    first = normalize_order_book(
        {"bids": [["99", "1"]], "asks": [["100", "1"]]},
        received_at=received_at,
        request_duration_seconds=0.1,
    )
    second = normalize_order_book(
        {"bids": [["99", "1"]], "asks": [["100", "2"]]},
        received_at=received_at,
        request_duration_seconds=0.1,
    )

    assert first["_market_data_id"] != second["_market_data_id"]


def test_ccxt_adapter_has_no_authenticated_trading_surface():
    for name in ("create_order", "fetch_order", "cancel_order", "fetch_balance", "fetch_positions", "fetch_fills"):
        assert not hasattr(CcxtAdapter, name)


def test_ccxt_adapter_uses_mainnet_client_without_sandbox_switch(monkeypatch):
    calls = {"load_markets": 0, "sandbox": 0}

    class FakeExchange:
        def __init__(self, options):
            assert options == {"enableRateLimit": True}

        def set_sandbox_mode(self, _enabled):
            calls["sandbox"] += 1

        def load_markets(self):
            calls["load_markets"] += 1

    monkeypatch.setattr("trade_common.exchange_adapters.ccxt_adapter.ccxt.binance", FakeExchange)

    CcxtAdapter(SimpleNamespace(exchange_id="binance"))

    assert calls == {"load_markets": 1, "sandbox": 0}
