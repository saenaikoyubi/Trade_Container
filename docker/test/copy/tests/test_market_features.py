from datetime import date, datetime, timezone
from dataclasses import replace
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trade_common.config import AccountSettings, ExchangeSettings, Settings
from trade_common.models import Base, DailyPnl, Fill, Order, Position
from trade_common.runner import PaperExecutor
from trade_common.risk import validate_instrument_quantity
from trade_common.valuation import ValuationError, calculate_account_balance


BYBIT = ExchangeSettings(
    exchange_id="bybit",
    adapter="ccxt",
    symbols=("BTCUSDT",),
    taker_fee_rate=Decimal("0.00055"),
    maker_fee_rate=Decimal("0.0002"),
    options={"defaultType": "linear"},
)
CONFIG = Settings(
    exchanges={"bybit": BYBIT},
    poll_interval_seconds=1,
    market_data_max_age_seconds=10,
    max_order_quantity=Decimal("10"),
    max_order_notional=Decimal("50000"),
    max_position_notional=Decimal("100000"),
    max_daily_loss=Decimal("5000"),
    max_price_deviation_pct=Decimal("0.05"),
    max_orders_per_minute=60,
    account=AccountSettings(initial_balance=Decimal("10000"), default_leverage=Decimal("10")),
)


class FakeAdapter:
    metadata_ready = True

    def __init__(self):
        self.fail_prices = False

    def resolve_symbol(self, symbol):
        if symbol in {"BTCUSDT", "BTC/USDT:USDT"}:
            return "BTCUSDT"
        raise ValueError(symbol)

    def fetch_instruments(self, symbol=None):
        if symbol is not None:
            symbol = self.resolve_symbol(symbol)
        return [
            {
                "exchange_id": "bybit",
                "symbol": "BTCUSDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "settle_asset": "USDT",
                "contract_type": "linear_perpetual",
                "contract_size": Decimal("1"),
                "qty_step": Decimal("0.001"),
                "min_qty": Decimal("0.001"),
                "max_qty": Decimal("100"),
                "max_market_qty": Decimal("50"),
                "price_step": Decimal("0.1"),
                "min_notional": Decimal("5"),
                "quantity_unit": "BTC",
                "status": "active",
            }
        ]

    def fetch_prices(self, symbols=None):
        if self.fail_prices:
            raise RuntimeError("price failure")
        requested = symbols or ["BTCUSDT"]
        return [
            {
                "exchange_id": "bybit",
                "symbol": self.resolve_symbol(symbol),
                "mark_price": Decimal("1100"),
                "mark_observed_at": datetime.now(timezone.utc),
                "last_price": Decimal("1100"),
                "bid_price": Decimal("1099"),
                "ask_price": Decimal("1101"),
                "mid_price": Decimal("1100"),
                "observed_at": datetime.now(timezone.utc),
            }
            for symbol in requested
        ]

    def fetch_order_book(self, symbol):
        self.resolve_symbol(symbol)
        if self.fail_prices:
            raise RuntimeError("price failure")
        return {
            "bids": [["1099", "10"]], "asks": [["1101", "10"]],
            "_received_at": datetime.now(timezone.utc),
            "_request_duration_seconds": 0.01,
        }

    def close(self):
        return None


class FakePool:
    def __init__(self, config, adapter):
        self.config = config
        self.adapter = adapter
        self._adapters = {"bybit": adapter}

    def get(self, exchange_id):
        assert exchange_id == "bybit"
        return self.adapter

    def close(self):
        return None


@pytest.fixture
def sessions():
    database = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(database)
    return sessionmaker(bind=database, expire_on_commit=False)


@pytest.fixture
def market_client(sessions, monkeypatch):
    import trade_api_service.main as api

    adapter = FakeAdapter()
    pool = FakePool(CONFIG, adapter)

    def get_test_session():
        with sessions() as session:
            yield session

    api.app.dependency_overrides[api.get_session] = get_test_session
    api.app.dependency_overrides[api.authenticate] = lambda: None
    monkeypatch.setattr(api, "settings", lambda: CONFIG)
    monkeypatch.setattr(api, "ExchangeAdapterPool", lambda _config: pool)
    try:
        with TestClient(api.app) as client:
            yield client, adapter
    finally:
        api.app.dependency_overrides.clear()


def test_decimal_step_validation_and_full_close_exemption():
    instrument = {
        "status": "active",
        "min_qty": Decimal("0.005"),
        "qty_step": Decimal("0.005"),
        "min_notional": Decimal("5"),
        "max_qty": None,
    }
    assert validate_instrument_quantity(Decimal("0.010"), Decimal("1000"), instrument).allowed
    assert not validate_instrument_quantity(Decimal("0.012"), Decimal("1000"), instrument).allowed
    instrument["min_qty"] = Decimal("0.25")
    instrument["qty_step"] = Decimal("0.25")
    assert validate_instrument_quantity(Decimal("0.50"), Decimal("100"), instrument).allowed
    assert not validate_instrument_quantity(Decimal("0.60"), Decimal("100"), instrument).allowed
    assert validate_instrument_quantity(
        Decimal("0.0001"), Decimal("1"), instrument, is_full_close=True
    ).allowed


def test_market_endpoints_alias_audit_idempotency_and_request_lookup(market_client, sessions):
    client, adapter = market_client
    instruments = client.get("/api/v1/instruments?exchange_id=bybit&symbol=BTC%2FUSDT%3AUSDT")
    prices = client.get("/api/v1/prices?exchange_id=bybit&symbols=BTC%2FUSDT%3AUSDT")
    payload = {
        "request_id": "alpha:one",
        "exchange_id": "bybit",
        "symbol": "BTC/USDT:USDT",
        "side": "buy",
        "order_type": "market",
        "quantity": "0.005",
        "strategy_id": "alpha",
    }
    created = client.post("/api/v1/orders", json=payload)
    adapter.fail_prices = True
    replayed = client.post("/api/v1/orders", json=payload)
    adapter.resolve_symbol = lambda _symbol: (_ for _ in ()).throw(RuntimeError("metadata outage"))
    canonical_replay = client.post("/api/v1/orders", json={**payload, "symbol": "BTCUSDT"})
    looked_up = client.get("/api/v1/orders/by-request-id/alpha:one")
    filtered = client.get("/api/v1/history/orders?strategy_id=alpha")
    invalid_id = client.post("/api/v1/orders", json={**payload, "request_id": "bad/id"})

    assert instruments.status_code == 200
    assert instruments.json()[0]["symbol"] == "BTCUSDT"
    assert instruments.json()[0]["max_market_qty"] == "50"
    assert instruments.json()[0]["quantity_unit"] == "BTC"
    assert prices.status_code == 200
    assert created.status_code == 202
    assert created.json()["symbol"] == "BTCUSDT"
    assert created.json()["strategy_id"] == "alpha"
    assert replayed.status_code == 200
    assert canonical_replay.status_code == 200
    assert looked_up.json()["id"] == created.json()["id"]
    assert [item["id"] for item in filtered.json()["items"]] == [created.json()["id"]]
    assert invalid_id.status_code == 422
    with sessions() as session:
        assert session.scalar(select(Order).where(Order.request_id == "alpha:one")).symbol == "BTCUSDT"


def test_balance_does_not_deduct_fee_twice(sessions):
    adapter = FakeAdapter()
    pool = FakePool(CONFIG, adapter)
    with sessions() as session:
        order = Order(
            request_id="balance-order",
            exchange_id="bybit",
            symbol="BTCUSDT",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
        )
        session.add(order)
        session.flush()
        session.add_all(
            [
                DailyPnl(trade_date=date(2026, 9, 10), realized_pnl=Decimal("10")),
                Fill(
                    order_id=order.id,
                    sequence=1,
                    exchange_id="bybit",
                    symbol="BTCUSDT",
                    side="buy",
                    quantity=Decimal("1"),
                    price=Decimal("1000"),
                    fee=Decimal("2"),
                ),
                Position(
                    exchange_id="bybit",
                    symbol="BTCUSDT",
                    quantity=Decimal("1"),
                    average_entry_price=Decimal("1000"),
                ),
            ]
        )
        session.flush()
        result = calculate_account_balance(session, CONFIG, pool)

    assert result.realized_pnl == Decimal("10")
    assert result.total_fee == Decimal("2")
    assert result.unrealized_pnl == Decimal("100")
    assert result.equity == Decimal("10110")
    assert result.used_margin == Decimal("110")
    assert result.available_balance == Decimal("10000")


def test_balance_rejects_unsupported_account_currency(sessions):
    unsupported = Settings(
        **{**CONFIG.__dict__, "account": AccountSettings(currency="EUR")}
    )
    with sessions() as session, pytest.raises(ValuationError, match="unsupported account currency"):
        calculate_account_balance(session, unsupported, FakePool(unsupported, FakeAdapter()))


def test_ready_fails_when_runtime_configuration_cannot_load(sessions, monkeypatch):
    import trade_api_service.main as api

    def get_test_session():
        with sessions() as session:
            yield session

    def fail_settings():
        raise ValueError("broken configuration")

    api.app.dependency_overrides[api.get_session] = get_test_session
    monkeypatch.setattr(api, "settings", fail_settings)
    try:
        with TestClient(api.app) as client:
            response = client.get("/ready")
    finally:
        api.app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json()["checks"]["configuration"] == {
        "ready": False,
        "last_error": "broken configuration",
    }


def test_full_close_dust_is_accepted(market_client, sessions):
    client, _adapter = market_client
    with sessions() as session:
        session.add(
            Position(
                exchange_id="bybit",
                symbol="BTCUSDT",
                quantity=Decimal("0.0005"),
                average_entry_price=Decimal("1000"),
            )
        )
        session.commit()
    response = client.post(
        "/api/v1/orders",
        json={
            "request_id": "dust-close",
            "exchange_id": "bybit",
            "symbol": "BTCUSDT",
            "side": "sell",
            "order_type": "market",
            "quantity": "0.0005",
            "reduce_only": True,
        },
    )
    assert response.status_code == 202


def test_bybit_close_request_splits_by_market_cap_and_can_be_canceled(market_client, sessions):
    client, _adapter = market_client
    with sessions() as session:
        session.add(Position(
            exchange_id="bybit", symbol="BTCUSDT",
            quantity=Decimal("51"), average_entry_price=Decimal("1000"),
        ))
        session.commit()

    created = client.post("/api/v1/positions/close", json={
        "request_id": "split-close", "exchange_id": "bybit", "symbol": "BTC/USDT:USDT",
    })
    assert created.status_code == 202
    assert created.json()["symbol"] == "BTCUSDT"
    assert [Decimal(item["quantity"]) for item in created.json()["items"]] == [Decimal("50"), Decimal("1")]
    assert all(item["reduce_only"] for item in created.json()["items"])

    blocked = client.post("/api/v1/orders", json={
        "request_id": "while-closing", "exchange_id": "bybit", "symbol": "BTCUSDT",
        "side": "sell", "order_type": "market", "quantity": "1", "reduce_only": True,
    })
    assert blocked.status_code == 409

    canceled = client.post("/api/v1/close-requests/split-close/cancel")
    assert canceled.status_code == 202
    assert canceled.json()["status"] == "canceling"
    assert all(item["cancellation_requested"] for item in canceled.json()["items"])
    looked_up = client.get("/api/v1/close-requests/split-close")
    assert looked_up.json()["status"] == "canceling"


def test_invalid_defined_max_qty_disables_new_orders(market_client):
    client, adapter = market_client
    original = adapter.fetch_instruments

    def invalid_max_qty(symbol=None):
        instruments = original(symbol)
        instruments[0]["max_qty"] = Decimal("0")
        return instruments

    adapter.fetch_instruments = invalid_max_qty
    instrument = client.get("/api/v1/instruments?exchange_id=bybit&symbol=BTCUSDT")
    assert instrument.status_code == 200
    assert instrument.json()[0]["new_or_increase_allowed"] is False
    assert instrument.json()[0]["new_or_increase_reason_code"] == "instrument_metadata_invalid"

    order = client.post("/api/v1/orders", json={
        "request_id": "invalid-max", "exchange_id": "bybit", "symbol": "BTCUSDT",
        "side": "buy", "order_type": "market", "quantity": "1",
    })
    assert order.status_code == 503
    assert order.json()["reason_code"] == "instrument_metadata_invalid"


def test_empty_bybit_defaults_still_allow_explicit_symbol(market_client):
    import trade_api_service.main as api

    client, _adapter = market_client
    config = replace(CONFIG, exchanges={"bybit": replace(BYBIT, symbols=())})

    class EmptyDefaultsAdapter(FakeAdapter):
        def fetch_instruments(self, symbol=None):
            return [] if symbol is None else super().fetch_instruments(symbol)

        def fetch_prices(self, symbols=None):
            return [] if symbols is None else super().fetch_prices(symbols)

    api.app.state.runtime_config = config
    api.app.state.adapter_pool = FakePool(config, EmptyDefaultsAdapter())

    assert client.get("/api/v1/exchanges").json()[0]["symbols"] == []
    assert client.get("/api/v1/instruments?exchange_id=bybit").json() == []
    assert client.get("/api/v1/instruments?exchange_id=bybit&symbol=BTCUSDT").status_code == 200
    assert client.get("/api/v1/prices?exchange_id=bybit&symbols=BTCUSDT").status_code == 200
    order = client.post("/api/v1/orders", json={
        "request_id": "explicit-btc", "exchange_id": "bybit", "symbol": "BTCUSDT",
        "side": "buy", "order_type": "market", "quantity": "0.01",
    })
    assert order.status_code == 202


def test_inactive_bybit_position_closes_at_fresh_mark_without_book(market_client, sessions):
    client, adapter = market_client
    original = adapter.fetch_instruments

    def inactive_instrument(symbol=None):
        items = original(symbol)
        items[0]["status"] = "inactive"
        return items

    adapter.fetch_instruments = inactive_instrument
    adapter.fetch_order_book = lambda _symbol: (_ for _ in ()).throw(RuntimeError("no book"))
    with sessions() as session:
        session.add(Position(
            exchange_id="bybit", symbol="BTCUSDT",
            quantity=Decimal("1"), average_entry_price=Decimal("1000"),
        ))
        session.commit()

    created = client.post("/api/v1/positions/close", json={
        "request_id": "stopped-close", "exchange_id": "bybit", "symbol": "BTCUSDT",
    })
    assert created.status_code == 202
    worker = PaperExecutor(
        CONFIG, adapters=FakePool(CONFIG, adapter), sessions=sessions,
        now=lambda: datetime.now(timezone.utc),
    )
    assert worker.process_once()

    completed = client.get("/api/v1/close-requests/stopped-close")
    assert completed.json()["status"] == "completed"
    with sessions() as session:
        assert session.scalar(select(Position).where(Position.symbol == "BTCUSDT")).quantity == Decimal("0")
        assert session.scalar(select(Fill).where(Fill.symbol == "BTCUSDT")).price == Decimal("1100")


def test_kill_switch_cancels_close_without_recording_fill(market_client, sessions):
    client, adapter = market_client
    with sessions() as session:
        session.add(Position(
            exchange_id="bybit", symbol="BTCUSDT",
            quantity=Decimal("1"), average_entry_price=Decimal("1000"),
        ))
        session.commit()
    created = client.post("/api/v1/positions/close", json={
        "request_id": "kill-close", "exchange_id": "bybit", "symbol": "BTCUSDT",
    })
    assert created.status_code == 202
    halted = client.post("/api/v1/kill-switch", json={"enabled": True, "reason": "test"})
    assert halted.status_code == 200
    assert halted.json()["cancellation_requested_count"] == 1

    worker = PaperExecutor(
        CONFIG, adapters=FakePool(CONFIG, adapter), sessions=sessions,
        now=lambda: datetime.now(timezone.utc),
    )
    assert worker.process_once()
    assert client.get("/api/v1/close-requests/kill-close").json()["status"] == "canceled"
    with sessions() as session:
        assert session.scalar(select(Position).where(Position.symbol == "BTCUSDT")).quantity == Decimal("1")
        assert list(session.scalars(select(Fill)).all()) == []


def test_removed_exchange_fails_affected_close_target(market_client, sessions):
    client, adapter = market_client
    with sessions() as session:
        session.add(Position(
            exchange_id="bybit", symbol="BTCUSDT",
            quantity=Decimal("1"), average_entry_price=Decimal("1000"),
        ))
        session.commit()
    created = client.post("/api/v1/positions/close", json={
        "request_id": "removed-exchange-close", "exchange_id": "bybit", "symbol": "BTCUSDT",
    })
    assert created.status_code == 202

    after_removal = replace(CONFIG, exchanges={})
    worker = PaperExecutor(
        after_removal, adapters=FakePool(after_removal, adapter), sessions=sessions,
        now=lambda: datetime.now(timezone.utc),
    )
    assert worker.process_once()
    parent = client.get("/api/v1/close-requests/removed-exchange-close").json()
    assert parent["status"] == "failed"
    assert parent["positions"][0]["reason_code"] == "exchange_not_configured"
    assert parent["items"][0]["status"] == "rejected"


def test_balance_api_is_fail_fast_and_health_marks_paper_mode(market_client, sessions):
    client, adapter = market_client
    with sessions() as session:
        session.add(
            Position(
                exchange_id="bybit",
                symbol="BTCUSDT",
                quantity=Decimal("1"),
                average_entry_price=Decimal("1000"),
            )
        )
        session.commit()

    health = client.get("/health")
    available = client.get("/api/v1/balance")
    adapter.fail_prices = True
    unavailable = client.get("/api/v1/balance")

    assert health.status_code == 200
    assert health.json()["environment"] == "paper"
    assert health.json()["simulation"] is True
    assert Decimal(available.json()["equity"]) == Decimal("10100")
    assert unavailable.status_code == 503


def test_order_creation_with_none_min_notional(market_client):
    client, adapter = market_client
    # Simulate Bybit Linear Perpetual where min_notional is None
    orig_fetch_instruments = adapter.fetch_instruments

    def fake_instruments_without_notional(symbol=None):
        items = orig_fetch_instruments(symbol)
        for item in items:
            item["min_notional"] = None
        return items

    adapter.fetch_instruments = fake_instruments_without_notional

    response = client.post(
        "/api/v1/orders",
        json={
            "request_id": "bybit-none-notional-order",
            "exchange_id": "bybit",
            "symbol": "BTCUSDT",
            "side": "buy",
            "order_type": "market",
            "quantity": "0.010",
            "strategy_id": "alpha-test",
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "pending"
    assert response.json()["symbol"] == "BTCUSDT"
