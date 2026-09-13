from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trade_common.config import ExchangeSettings, Settings
from trade_common.models import Base, ControlFlag, Order, Position


EXCHANGE = ExchangeSettings(
    exchange_id="fake",
    adapter="ccxt",
    symbols=("BTC/USD", "ETH/USD"),
    taker_fee_rate=Decimal("0.001"),
    maker_fee_rate=Decimal("0.001"),
)
CONFIG = Settings(
    exchanges={"fake": EXCHANGE},
    poll_interval_seconds=1,
    market_data_max_age_seconds=10,
    max_order_quantity=Decimal("2"),
    max_order_notional=Decimal("1000"),
    max_position_notional=Decimal("2000"),
    max_daily_loss=Decimal("100"),
    max_price_deviation_pct=Decimal("0.05"),
    max_orders_per_minute=20,
)


class FakeAdapter:
    def fetch_instruments(self, symbol=None):
        return [
            {
                "exchange_id": "fake",
                "symbol": symbol,
                "status": "active",
                "min_qty": Decimal("0.01"),
                "qty_step": Decimal("0.01"),
                "max_qty": Decimal("100"),
                "min_notional": Decimal("1"),
            }
        ]

    def fetch_prices(self, symbols=None):
        return [
            {
                "exchange_id": "fake",
                "symbol": symbol,
                "mid_price": Decimal("100"),
                "observed_at": datetime.now(timezone.utc),
            }
            for symbol in (symbols or [])
        ]

    def close(self):
        return None


class FakePool:
    def __init__(self, config):
        self.config = config
        self.adapter = FakeAdapter()
        self._adapters = {"fake": self.adapter}

    def get(self, exchange_id):
        assert exchange_id == "fake"
        return self.adapter

    def close(self):
        self.adapter.close()


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def client(sessions, monkeypatch):
    import trade_api_service.main as api

    def get_test_session():
        session = sessions()
        try:
            yield session
        finally:
            session.close()

    api.app.dependency_overrides[api.get_session] = get_test_session
    api.app.dependency_overrides[api.authenticate] = lambda: None
    monkeypatch.setattr(api, "settings", lambda: CONFIG)
    monkeypatch.setattr(api, "ExchangeAdapterPool", FakePool)
    try:
        with TestClient(api.app) as test_client:
            yield test_client
    finally:
        api.app.dependency_overrides.clear()


def order_payload(request_id: str, *, reduce_only: bool = False):
    return {
        "request_id": request_id,
        "exchange_id": "fake",
        "symbol": "BTC/USD",
        "side": "buy",
        "order_type": "market",
        "quantity": "1",
        "reduce_only": reduce_only,
    }


def test_close_only_cancels_opening_orders_and_rejects_new_ones(sessions, client):
    created = client.post("/api/v1/orders", json=order_payload("opening-before-control"))
    assert created.status_code == 202

    enabled = client.post(
        "/api/v1/close-only",
        json={"enabled": True, "reason": "manual emergency close"},
    )
    assert enabled.status_code == 200
    assert enabled.json()["close_only"] is True
    assert enabled.json()["kill_switch"] is False
    assert enabled.json()["cancellation_requested_count"] == 1

    rejected = client.post("/api/v1/orders", json=order_payload("opening-after-control"))
    closing = client.post(
        "/api/v1/orders",
        json=order_payload("closing-after-control", reduce_only=True),
    )
    control = client.get("/api/v1/trading-control")

    assert rejected.status_code == 409
    assert "close-only" in rejected.json()["detail"]
    assert closing.status_code == 202
    assert control.json()["close_only"] is True

    with sessions() as session:
        existing = session.scalar(select(Order).where(Order.request_id == "opening-before-control"))
        assert existing.cancellation_requested is True


def test_kill_switch_takes_precedence_over_close_only(client):
    client.post("/api/v1/close-only", json={"enabled": True, "reason": "close"})
    halted = client.post("/api/v1/kill-switch", json={"enabled": True, "reason": "halt"})

    assert halted.status_code == 200
    assert halted.json()["kill_switch"] is True
    assert halted.json()["close_only"] is False
    assert halted.json()["reason"] == "halt"


def test_close_positions_creates_reduce_only_orders_and_is_idempotent(sessions, client):
    with sessions() as session:
        session.add_all(
            [
                Position(
                    exchange_id="fake",
                    symbol="BTC/USD",
                    quantity=Decimal("1.25"),
                    average_entry_price=Decimal("100"),
                ),
                Position(
                    exchange_id="fake",
                    symbol="ETH/USD",
                    quantity=Decimal("-2"),
                    average_entry_price=Decimal("50"),
                ),
                Order(
                    request_id="existing-open-order",
                    exchange_id="fake",
                    exchange_network="mainnet",
                    symbol="BTC/USD",
                    side="buy",
                    order_type="limit",
                    quantity=Decimal("1"),
                    limit_price=Decimal("90"),
                    status="open",
                ),
            ]
        )
        session.commit()

    payload = {
        "request_id": "close-operation-1",
        "exchange_id": "fake",
    }
    created = client.post("/api/v1/positions/close", json=payload)
    replayed = client.post("/api/v1/positions/close", json=payload)

    assert created.status_code == 202
    assert replayed.status_code == 202
    created_items = created.json()["items"]
    replayed_items = replayed.json()["items"]
    assert [item["id"] for item in replayed_items] == [item["id"] for item in created_items]
    assert {
        (item["symbol"], item["side"], Decimal(item["quantity"]), item["reduce_only"])
        for item in created_items
    } == {
        ("BTC/USD", "sell", Decimal("1.25"), True),
        ("ETH/USD", "buy", Decimal("2"), True),
    }

    with sessions() as session:
        existing = session.scalar(select(Order).where(Order.request_id == "existing-open-order"))
        assert existing.cancellation_requested is True


def test_close_positions_supports_one_symbol_and_rejects_empty_position(sessions, client):
    with sessions() as session:
        session.add(
            Position(
                exchange_id="fake",
                symbol="BTC/USD",
                quantity=Decimal("-0.5"),
                average_entry_price=Decimal("100"),
            )
        )
        session.commit()

    created = client.post(
        "/api/v1/positions/close",
        json={
            "request_id": "close-one",
            "exchange_id": "fake",
            "symbol": "BTC/USD",
        },
    )
    missing = client.post(
        "/api/v1/positions/close",
        json={
            "request_id": "close-missing",
            "exchange_id": "fake",
            "symbol": "ETH/USD",
        },
    )

    assert created.status_code == 202
    assert created.json()["items"][0]["side"] == "buy"
    assert missing.status_code == 409
    assert missing.json()["detail"] == "no open position exists"


def test_close_positions_accepts_128_character_id_without_overflow(sessions, client):
    with sessions() as session:
        session.add(
            Position(
                exchange_id="fake",
                symbol="BTC/USD",
                quantity=Decimal("1"),
                average_entry_price=Decimal("100"),
            )
        )
        session.commit()

    payload = {"request_id": "r" * 128, "exchange_id": "fake", "symbol": "BTC/USD"}
    created = client.post("/api/v1/positions/close", json=payload)
    replayed = client.post("/api/v1/positions/close", json=payload)

    assert created.status_code == 202
    assert replayed.status_code == 202
    assert replayed.json()["items"][0]["id"] == created.json()["items"][0]["id"]
    assert len(created.json()["items"][0]["request_id"]) <= 128
