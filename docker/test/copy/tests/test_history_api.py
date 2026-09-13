from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trade_common.config import ExchangeSettings, Settings
from trade_common.models import Base, DailyPnl, Fill, Order


ACTIVE_EXCHANGE = ExchangeSettings(
    exchange_id="active",
    adapter="ccxt",
    symbols=("BTCUSDT",),
    taker_fee_rate=Decimal("0.001"),
    maker_fee_rate=Decimal("0.001"),
)
OTHER_EXCHANGE = ExchangeSettings(
    exchange_id="other",
    adapter="ccxt",
    symbols=("BTCUSDT",),
    taker_fee_rate=Decimal("0.001"),
    maker_fee_rate=Decimal("0.001"),
)
CONFIG = Settings(
    exchanges={"active": ACTIVE_EXCHANGE, "other": OTHER_EXCHANGE},
    poll_interval_seconds=1,
    market_data_max_age_seconds=10,
    max_order_quantity=Decimal("10"),
    max_order_notional=Decimal("1000000"),
    max_position_notional=Decimal("1000000"),
    max_daily_loss=Decimal("1000000"),
    max_price_deviation_pct=Decimal("0.50"),
    max_orders_per_minute=100,
)


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
    try:
        with TestClient(api.app) as test_client:
            yield test_client
    finally:
        api.app.dependency_overrides.clear()


def test_pnl_history_fills_missing_days_and_accumulates(sessions, client):
    with sessions() as session:
        session.add_all(
            [
                DailyPnl(trade_date=date(2026, 7, 1), realized_pnl=Decimal("10.5")),
                DailyPnl(trade_date=date(2026, 7, 3), realized_pnl=Decimal("-4.25")),
            ]
        )
        session.commit()

    response = client.get("/api/v1/history/pnl?from=2026-07-01&to=2026-07-03")

    assert response.status_code == 200
    payload = response.json()
    assert payload["timezone"] == "UTC"
    assert [point["trade_date"] for point in payload["points"]] == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert [Decimal(point["daily_realized_pnl"]) for point in payload["points"]] == [
        Decimal("10.5"),
        Decimal("0"),
        Decimal("-4.25"),
    ]
    assert [Decimal(point["cumulative_realized_pnl"]) for point in payload["points"]] == [
        Decimal("10.5"),
        Decimal("10.5"),
        Decimal("6.25"),
    ]


def test_pnl_history_rejects_reversed_range(client):
    response = client.get("/api/v1/history/pnl?from=2026-07-03&to=2026-07-01")
    assert response.status_code == 422


def test_order_history_filters_and_uses_cursor(sessions, client):
    with sessions() as session:
        session.add_all(
            [
                Order(
                    id="order-new",
                    request_id="request-new",
                    exchange_id="fake",
                    exchange_network="mainnet",
                    symbol="BTC/USDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    status="filled",
                    created_at=datetime(2026, 7, 3, 12, tzinfo=timezone.utc),
                    updated_at=datetime(2026, 7, 3, 12, tzinfo=timezone.utc),
                ),
                Order(
                    id="order-old",
                    request_id="request-old",
                    exchange_id="fake",
                    exchange_network="mainnet",
                    symbol="BTC/USDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    status="filled",
                    created_at=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
                    updated_at=datetime(2026, 7, 2, 12, tzinfo=timezone.utc),
                ),
                Order(
                    id="order-other-symbol",
                    request_id="request-other-symbol",
                    exchange_id="fake",
                    exchange_network="mainnet",
                    symbol="ETH/USDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    status="filled",
                    created_at=datetime(2026, 7, 3, 13, tzinfo=timezone.utc),
                    updated_at=datetime(2026, 7, 3, 13, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()

    first = client.get(
        "/api/v1/history/orders?from=2026-07-01&to=2026-07-03&exchange_id=fake&symbol=BTC%2FUSDT&limit=1"
    )
    assert first.status_code == 200
    assert [item["id"] for item in first.json()["items"]] == ["order-new"]
    cursor = first.json()["next_cursor"]
    assert cursor

    second = client.get(
        "/api/v1/history/orders",
        params={
            "from": "2026-07-01",
            "to": "2026-07-03",
            "exchange_id": "fake",
            "symbol": "BTC/USDT",
            "limit": 1,
            "cursor": cursor,
        },
    )
    assert second.status_code == 200
    assert [item["id"] for item in second.json()["items"]] == ["order-old"]
    assert second.json()["next_cursor"] is None


def test_fill_history_applies_date_filter(sessions, client):
    with sessions() as session:
        order = Order(
            id="order-1",
            request_id="request-1",
            exchange_id="fake",
            exchange_network="mainnet",
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
        )
        session.add(order)
        session.flush()
        session.add_all(
            [
                Fill(
                    id="fill-in-range",
                    order_id=order.id,
                    sequence=1,
                    exchange_id="fake",
                    symbol="BTC/USDT",
                    side="buy",
                    quantity=Decimal("1"),
                    price=Decimal("100"),
                    fee=Decimal("0.4"),
                    executed_at=datetime(2026, 7, 2, 1, tzinfo=timezone.utc),
                ),
                Fill(
                    id="fill-out-of-range",
                    order_id=order.id,
                    sequence=2,
                    exchange_id="fake",
                    symbol="BTC/USDT",
                    side="buy",
                    quantity=Decimal("1"),
                    price=Decimal("101"),
                    fee=Decimal("0.4"),
                    executed_at=datetime(2026, 7, 3, 2, tzinfo=timezone.utc),
                ),
            ]
        )
        session.commit()

    response = client.get(
        "/api/v1/history/fills?from=2026-07-02&to=2026-07-02&exchange_id=fake&symbol=BTC%2FUSDT"
    )
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == ["fill-in-range"]


def test_history_rejects_invalid_cursor(client):
    response = client.get("/api/v1/history/orders?cursor=not-a-cursor")
    assert response.status_code == 422


def test_history_unknown_symbols_return_empty_pages(client):
    configured = client.get(
        "/api/v1/history/orders?exchange_id=active&symbol=NOT-CONFIGURED"
    )
    exchange_less = client.get("/api/v1/history/fills?symbol=NOT-CONFIGURED")

    assert configured.status_code == 200
    assert configured.json() == {"items": [], "next_cursor": None}
    assert exchange_less.status_code == 200
    assert exchange_less.json() == {"items": [], "next_cursor": None}


def test_history_uses_cached_alias_and_keeps_canonical_scoped_to_exchange(sessions, client):
    import trade_api_service.main as api

    class CachedAdapter:
        @staticmethod
        def resolve_cached_symbol(symbol):
            return "BTCUSDT" if symbol == "BTC/USDT:USDT" else None

    api.app.state.adapter_pool._adapters["active"] = CachedAdapter()
    created_at = datetime(2026, 7, 4, 12, tzinfo=timezone.utc)
    with sessions() as session:
        session.add_all(
            [
                Order(
                    id="active-canonical",
                    request_id="active-canonical",
                    exchange_id="active",
                    exchange_network="mainnet",
                    symbol="BTCUSDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    created_at=created_at,
                    updated_at=created_at,
                ),
                Order(
                    id="active-legacy",
                    request_id="active-legacy",
                    exchange_id="active",
                    exchange_network="mainnet",
                    symbol="BTC/USDT:USDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    created_at=created_at,
                    updated_at=created_at,
                ),
                Order(
                    id="retired-legacy",
                    request_id="retired-legacy",
                    exchange_id="retired",
                    exchange_network="mainnet",
                    symbol="BTC/USDT:USDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    created_at=created_at,
                    updated_at=created_at,
                ),
                Order(
                    id="other-canonical",
                    request_id="other-canonical",
                    exchange_id="other",
                    exchange_network="mainnet",
                    symbol="BTCUSDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    created_at=created_at,
                    updated_at=created_at,
                ),
            ]
        )
        session.commit()

    response = client.get("/api/v1/history/orders?symbol=BTC%2FUSDT%3AUSDT")
    ids = {item["id"] for item in response.json()["items"]}

    assert response.status_code == 200
    assert ids == {"active-canonical", "active-legacy", "retired-legacy"}


def test_history_never_initializes_adapter_and_rejects_surrounding_whitespace(client):
    import trade_api_service.main as api

    api.app.state.adapter_pool.get = lambda _exchange_id: (_ for _ in ()).throw(
        AssertionError("history must not initialize an adapter")
    )

    unknown_alias = client.get("/api/v1/history/orders?exchange_id=active&symbol=BTC%2FUSDT%3AUSDT")
    malformed = client.get("/api/v1/history/orders?symbol=%20BTCUSDT%20")

    assert unknown_alias.status_code == 200
    assert unknown_alias.json() == {"items": [], "next_cursor": None}
    assert malformed.status_code == 422
