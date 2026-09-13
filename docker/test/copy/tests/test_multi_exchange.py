from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trade_common.config import ExchangeSettings, Settings
from trade_common.exchange_adapters.pool import ExchangeAdapterPool
from trade_common.models import Base, Order
from trade_common.runner import PaperExecutor


NOW = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
BINANCE = ExchangeSettings(
    exchange_id="binance",
    adapter="ccxt",
    symbols=("BTC/USDT",),
    taker_fee_rate=Decimal("0.004"),
    maker_fee_rate=Decimal("0.002"),
)
DYDX = ExchangeSettings(
    exchange_id="dydx",
    adapter="dydx",
    symbols=("BTC-USD",),
    taker_fee_rate=Decimal("0.003"),
    maker_fee_rate=Decimal("0.001"),
)
CONFIG = Settings(
    exchanges={"binance": BINANCE, "dydx": DYDX},
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


class FakeAdapter:
    def __init__(self, book: dict | None = None, *, fails: bool = False, allowed_symbols=None):
        self.book = book
        self.fails = fails
        self.closed = False
        self.allowed_symbols = set(allowed_symbols or {"BTC/USDT", "BTC-USD"})

    def resolve_symbol(self, symbol: str) -> str:
        if symbol not in self.allowed_symbols:
            raise ValueError(symbol)
        return symbol

    def fetch_order_book(self, _symbol: str) -> dict:
        if self.fails:
            raise RuntimeError("exchange unavailable")
        return dict(self.book or {})

    def fetch_instruments(self, symbol: str | None = None) -> list[dict]:
        return [
            {
                "symbol": symbol,
                "status": "active",
                "min_qty": Decimal("1"),
                "qty_step": Decimal("1"),
                "max_qty": Decimal("100"),
                "min_notional": Decimal("1"),
            }
        ]

    def fetch_prices(self, symbols=None) -> list[dict]:
        return [{"symbol": symbol, "mid_price": Decimal("100")} for symbol in (symbols or [])]

    def close(self):
        self.closed = True


class FakePool:
    def __init__(self, adapters, config=CONFIG):
        self.adapters = adapters
        self._adapters = adapters
        self.config = config

    def get(self, exchange_id):
        return self.adapters[exchange_id]

    def close(self):
        for adapter in self.adapters.values():
            adapter.close()


def order_book(identity: str) -> dict:
    return {
        "bids": [["99", "10"]],
        "asks": [["101", "10"]],
        "timestamp": None,
        "_market_data_id": identity,
        "_received_at": NOW,
        "_request_duration_seconds": 0,
    }


def test_adapter_pool_creates_each_exchange_lazily_and_reuses_it():
    created = []
    adapters = {}

    def factory(exchange_config):
        created.append(exchange_config.exchange_id)
        adapter = FakeAdapter()
        adapters[exchange_config.exchange_id] = adapter
        return adapter

    pool = ExchangeAdapterPool(CONFIG, factory=factory)

    assert pool.get("binance") is pool.get("binance")
    assert pool.get("dydx") is pool.get("dydx")
    assert created == ["binance", "dydx"]

    pool.close()
    assert all(adapter.closed for adapter in adapters.values())


def test_failed_exchange_backs_off_without_blocking_another_exchange(sessions):
    with sessions() as session:
        session.add_all(
            [
                Order(
                    request_id="dydx-first",
                    exchange_id="dydx",
                    exchange_network="mainnet",
                    symbol="BTC-USD",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    next_attempt_at=NOW,
                    created_at=NOW,
                ),
                Order(
                    request_id="binance-second",
                    exchange_id="binance",
                    exchange_network="mainnet",
                    symbol="BTC/USDT",
                    side="buy",
                    order_type="market",
                    quantity=Decimal("1"),
                    next_attempt_at=NOW,
                    created_at=NOW + timedelta(milliseconds=1),
                ),
            ]
        )
        session.commit()

    worker = PaperExecutor(
        CONFIG,
        adapters=FakePool(
            {
                "dydx": FakeAdapter(fails=True),
                "binance": FakeAdapter(order_book("binance-book")),
            }
        ),
        sessions=sessions,
        now=lambda: NOW,
    )

    assert worker.process_once() is True
    assert worker.process_once() is True

    with sessions() as session:
        dydx_order = session.query(Order).filter_by(request_id="dydx-first").one()
        binance_order = session.query(Order).filter_by(request_id="binance-second").one()
        assert dydx_order.status == "pending"
        assert dydx_order.retry_count == 1
        assert dydx_order.next_attempt_at.replace(tzinfo=timezone.utc) > NOW
        assert binance_order.status == "filled"


def test_order_api_validates_exchange_symbol_and_idempotency(sessions, monkeypatch):
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
    monkeypatch.setattr(
        api,
        "ExchangeAdapterPool",
        lambda config: FakePool(
            {
                "binance": FakeAdapter(order_book("api-binance"), allowed_symbols={"BTC/USDT"}),
                "dydx": FakeAdapter(order_book("api-dydx"), allowed_symbols={"BTC-USD"}),
            },
            config,
        ),
    )
    payload = {
        "request_id": "api-multi-1",
        "exchange_id": "binance",
        "symbol": "BTC/USDT",
        "side": "buy",
        "order_type": "market",
        "quantity": "1",
    }
    try:
        with TestClient(api.app) as client:
            exchanges = client.get("/api/v1/exchanges")
            created = client.post("/api/v1/orders", json=payload)
            replayed = client.post("/api/v1/orders", json=payload)
            conflict = client.post(
                "/api/v1/orders",
                json={**payload, "exchange_id": "dydx", "symbol": "BTC-USD"},
            )
            invalid_symbol = client.post(
                "/api/v1/orders",
                json={**payload, "request_id": "api-multi-2", "symbol": "BTC-USD"},
            )
            invalid_exchange = client.post(
                "/api/v1/orders",
                json={**payload, "request_id": "api-multi-3", "exchange_id": "unknown"},
            )

        assert exchanges.status_code == 200
        assert [item["exchange_id"] for item in exchanges.json()] == ["binance", "dydx"]
        assert created.status_code == 202
        assert replayed.status_code == 200
        assert conflict.status_code == 409
        assert invalid_symbol.status_code == 422
        assert invalid_exchange.status_code == 422
    finally:
        api.app.dependency_overrides.clear()
