from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trade_common.config import ExchangeSettings, Settings
from trade_common.models import Base, Position


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


class FakeMarketAdapter:
    exchange_id = "fake"

    def __init__(self, *, fail_symbols=()):
        self.fail_symbols = set(fail_symbols)
        self.closed = False

    def market(self, symbol):
        return {"base": symbol.split("/")[0], "quote": symbol.split("/")[1]}

    def fetch_order_book(self, symbol):
        if symbol in self.fail_symbols:
            raise RuntimeError("test exchange failure")
        prices = {"BTC/USD": ("109", "111"), "ETH/USD": ("89", "91")}
        bid, ask = prices[symbol]
        return {
            "bids": [[bid, "1"]],
            "asks": [[ask, "1"]],
            "_received_at": datetime.now(timezone.utc),
            "_request_duration_seconds": 0.01,
        }

    def close(self):
        self.closed = True


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
def api_client(sessions, monkeypatch):
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
    try:
        with TestClient(api.app) as client:
            yield client, api
    finally:
        api.app.dependency_overrides.clear()


def seed_positions(sessions):
    updated_at = datetime(2026, 7, 20, 1, tzinfo=timezone.utc)
    with sessions() as session:
        session.add_all(
            [
                Position(
                    exchange_id="fake",
                    symbol="BTC/USD",
                    quantity=Decimal("1"),
                    average_entry_price=Decimal("100"),
                    updated_at=updated_at,
                ),
                Position(
                    exchange_id="fake",
                    symbol="ETH/USD",
                    quantity=Decimal("-1"),
                    average_entry_price=Decimal("100"),
                    updated_at=updated_at,
                ),
                Position(
                    exchange_id="fake",
                    symbol="ZERO/USD",
                    quantity=Decimal("0"),
                    average_entry_price=Decimal("0"),
                    updated_at=updated_at,
                ),
                Position(
                    exchange_id="other",
                    symbol="BTC/USD",
                    quantity=Decimal("5"),
                    average_entry_price=Decimal("1"),
                    updated_at=updated_at,
                ),
            ]
        )
        session.commit()


def test_current_positions_values_long_and_short_without_writing_database(sessions, api_client, monkeypatch):
    client, api = api_client
    seed_positions(sessions)
    adapter = FakeMarketAdapter()
    monkeypatch.setattr(api, "_public_exchange", lambda _config: adapter)

    response = client.get("/api/v1/current-positions?exchange_id=fake")

    assert response.status_code == 200
    payload = response.json()
    assert payload["valuation_complete"] is True
    assert payload["unpriced_count"] == 0
    assert Decimal(payload["total_unrealized_pnl"]) == Decimal("20")
    assert [item["symbol"] for item in payload["positions"]] == ["BTC/USD", "ETH/USD"]
    assert [item["position_side"] for item in payload["positions"]] == ["buy", "sell"]
    assert [Decimal(item["unrealized_pnl"]) for item in payload["positions"]] == [Decimal("10"), Decimal("10")]
    assert payload["positions"][0]["base_asset"] == "BTC"
    assert payload["positions"][0]["quote_asset"] == "USD"
    assert adapter.closed is False

    with sessions() as session:
        btc = session.scalar(select(Position).where(Position.exchange_id == "fake", Position.symbol == "BTC/USD"))
        assert btc.quantity == Decimal("1")
        assert btc.average_entry_price == Decimal("100")


def test_current_positions_marks_partial_failure_and_omits_total(sessions, api_client, monkeypatch):
    client, api = api_client
    seed_positions(sessions)
    adapter = FakeMarketAdapter(fail_symbols={"ETH/USD"})
    monkeypatch.setattr(api, "_public_exchange", lambda _config: adapter)

    response = client.get("/api/v1/current-positions?exchange_id=fake")

    assert response.status_code == 200
    payload = response.json()
    assert payload["valuation_complete"] is False
    assert payload["unpriced_count"] == 1
    assert payload["total_unrealized_pnl"] is None
    failed = next(item for item in payload["positions"] if item["symbol"] == "ETH/USD")
    assert failed["valuation_status"] == "unavailable"
    assert failed["current_price"] is None
    assert failed["unrealized_pnl"] is None


def test_current_positions_validates_symbol_and_returns_empty_snapshot(sessions, api_client, monkeypatch):
    client, api = api_client
    monkeypatch.setattr(api, "_public_exchange", lambda _config: (_ for _ in ()).throw(AssertionError()))

    rejected = client.get("/api/v1/current-positions?exchange_id=fake&symbol=NOT%2FALLOWED")
    empty = client.get("/api/v1/current-positions?exchange_id=fake&symbol=BTC%2FUSD")

    assert rejected.status_code == 422
    assert empty.status_code == 200
    assert empty.json()["positions"] == []
    assert Decimal(empty.json()["total_unrealized_pnl"]) == Decimal("0")


def test_current_positions_requires_exchange_id(api_client):
    client, _api = api_client
    response = client.get("/api/v1/current-positions")
    assert response.status_code == 422
