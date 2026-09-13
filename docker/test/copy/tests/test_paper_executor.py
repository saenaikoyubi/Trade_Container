from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from trade_common.config import ExchangeSettings, Settings
from trade_common.models import Base, Fill, Order, Position
from trade_common.runner import PaperExecutor


NOW = datetime(2026, 7, 22, 0, 0, tzinfo=timezone.utc)
EXCHANGE = ExchangeSettings(
    exchange_id="fake",
    adapter="ccxt",
    symbols=("BTC/USD",),
    taker_fee_rate=Decimal("0.004"),
    maker_fee_rate=Decimal("0.002"),
)
CONFIG = Settings(
    exchanges={"fake": EXCHANGE},
    poll_interval_seconds=0,
    market_data_max_age_seconds=10,
    max_order_quantity=Decimal("10"),
    max_order_notional=Decimal("1000000"),
    max_position_notional=Decimal("1000000"),
    max_daily_loss=Decimal("1000000"),
    max_price_deviation_pct=Decimal("0.50"),
    max_orders_per_minute=100,
)


def instrument_metadata(**overrides) -> dict:
    item = {
        "exchange_id": "fake",
        "symbol": "BTC/USD",
        "status": "active",
        "min_qty": Decimal("1"),
        "qty_step": Decimal("1"),
        "max_qty": Decimal("100"),
        "min_notional": Decimal("1"),
    }
    item.update(overrides)
    return item


class FakeExchange:
    def __init__(self, *books: dict):
        self.books = list(books)
        self.fetch_count = 0

    def fetch_order_book(self, _symbol: str) -> dict:
        if not self.books:
            raise AssertionError("no fake order book remains")
        self.fetch_count += 1
        return self.books.pop(0)

    def fetch_instruments(self, symbol: str | None = None) -> list[dict]:
        return [instrument_metadata(symbol=symbol or "BTC/USD")]

    def fetch_prices(self, symbols=None) -> list[dict]:
        return [
            {
                "exchange_id": "fake",
                "symbol": symbol,
                "mid_price": Decimal("100"),
                "observed_at": NOW,
            }
            for symbol in (symbols or ["BTC/USD"])
        ]

    def close(self):
        return None


class MetadataSequenceExchange(FakeExchange):
    def __init__(self, metadata_results, *books: dict):
        super().__init__(*books)
        self.metadata_results = list(metadata_results)

    def fetch_instruments(self, _symbol: str | None = None) -> list[dict]:
        if not self.metadata_results:
            raise AssertionError("no fake metadata result remains")
        result = self.metadata_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeAdapterPool:
    def __init__(self, adapters: dict[str, FakeExchange], config=CONFIG):
        self.adapters = adapters
        self._adapters = adapters
        self.config = config

    def get(self, exchange_id: str) -> FakeExchange:
        return self.adapters[exchange_id]

    def close(self):
        for adapter in self.adapters.values():
            adapter.close()


def book(
    identity: str,
    *,
    bids: list | None = None,
    asks: list | None = None,
    received_at: datetime = NOW,
    timestamp: int | None = None,
    duration: float = 0,
) -> dict:
    return {
        "bids": bids or [["99", "10"]],
        "asks": asks or [["101", "10"]],
        "timestamp": timestamp,
        "_market_data_id": identity,
        "_received_at": received_at,
        "_request_duration_seconds": duration,
    }


@pytest.fixture
def sessions():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def add_order(sessions, **values) -> str:
    with sessions() as session:
        order = Order(
            request_id=values.pop("request_id", "request-1"),
            exchange_id="fake",
            exchange_network="mainnet",
            symbol="BTC/USD",
            side=values.pop("side", "buy"),
            order_type=values.pop("order_type", "market"),
            quantity=values.pop("quantity", Decimal("1")),
            next_attempt_at=values.pop("next_attempt_at", NOW),
            **values,
        )
        session.add(order)
        session.commit()
        return order.id


def executor(sessions, exchange: FakeExchange) -> PaperExecutor:
    return PaperExecutor(
        CONFIG,
        adapters=FakeAdapterPool({"fake": exchange}),
        sessions=sessions,
        now=lambda: NOW,
    )


def test_market_partial_fill_cancels_remainder(sessions):
    order_id = add_order(sessions, quantity=Decimal("2"))
    worker = executor(sessions, FakeExchange(book("book-1", asks=[["100", "1"]])))

    assert worker.process_once() is True

    with sessions() as session:
        order = session.get(Order, order_id)
        fill = session.scalar(select(Fill).where(Fill.order_id == order_id))
        assert order.status == "canceled"
        assert order.filled_quantity == Decimal("1")
        assert fill.liquidity_role == "taker"
        assert fill.market_data_id == "book-1"


def test_limit_rests_then_fills_as_maker(sessions):
    order_id = add_order(sessions, order_type="limit", limit_price=Decimal("100"))
    exchange = FakeExchange(
        book("book-1", asks=[["101", "10"]]),
        book("book-2", asks=[["100", "10"]]),
    )
    worker = executor(sessions, exchange)

    worker.process_once()
    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "open"
        assert order.resting_since.replace(tzinfo=timezone.utc) == NOW

    worker.process_once()
    with sessions() as session:
        order = session.get(Order, order_id)
        fill = session.scalar(select(Fill).where(Fill.order_id == order_id))
        assert order.status == "filled"
        assert fill.liquidity_role == "maker"
        assert fill.fee.quantize(Decimal("0.001")) == Decimal("0.200")


def test_marketable_limit_fills_as_taker(sessions):
    order_id = add_order(sessions, order_type="limit", limit_price=Decimal("100"))
    worker = executor(sessions, FakeExchange(book("book-1", asks=[["100", "10"]])))

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        fill = session.scalar(select(Fill).where(Fill.order_id == order_id))
        assert order.status == "filled"
        assert fill.liquidity_role == "taker"


def test_same_snapshot_is_not_consumed_twice(sessions):
    order_id = add_order(
        sessions,
        order_type="limit",
        quantity=Decimal("2"),
        limit_price=Decimal("100"),
    )
    same = book("book-1", asks=[["100", "1"]])
    worker = executor(sessions, FakeExchange(same, dict(same)))

    worker.process_once()
    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        fills = session.scalars(select(Fill).where(Fill.order_id == order_id)).all()
        assert order.status == "partially_filled"
        assert order.filled_quantity == Decimal("1")
        assert len(fills) == 1


def test_open_limit_can_be_canceled_before_next_book(sessions):
    order_id = add_order(sessions, order_type="limit", limit_price=Decimal("100"))
    exchange = FakeExchange(book("book-1", asks=[["101", "10"]]))
    worker = executor(sessions, exchange)
    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        order.cancellation_requested = True
        session.commit()

    worker.process_once()
    with sessions() as session:
        assert session.get(Order, order_id).status == "canceled"
        assert exchange.fetch_count == 1


def test_stale_local_receive_time_defers_market_order(sessions):
    order_id = add_order(sessions)
    stale = book("book-1", received_at=NOW - timedelta(seconds=11))
    worker = executor(sessions, FakeExchange(stale))

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "pending"
        assert order.filled_quantity == Decimal("0")
        assert order.rejection_reason.startswith("market data is stale")


def test_stale_exchange_timestamp_defers_market_order(sessions):
    order_id = add_order(sessions)
    timestamp = int((NOW - timedelta(seconds=11)).timestamp() * 1000)
    stale = book("book-1", timestamp=timestamp)
    worker = executor(sessions, FakeExchange(stale))

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "pending"
        assert order.filled_quantity == Decimal("0")
        assert order.rejection_reason.startswith("market data is stale")


def test_slow_order_book_request_defers_market_order(sessions):
    order_id = add_order(sessions)
    slow = book("book-1", duration=11)
    worker = executor(sessions, FakeExchange(slow))

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "pending"
        assert order.filled_quantity == Decimal("0")
        assert order.rejection_reason.startswith("market data request was too slow")


def test_missing_exchange_timestamp_uses_receive_time(sessions):
    order_id = add_order(sessions)
    worker = executor(sessions, FakeExchange(book("book-1", asks=[["100", "2"]])))

    worker.process_once()

    with sessions() as session:
        assert session.get(Order, order_id).status == "filled"


def test_missing_instrument_metadata_surface_is_deferred(sessions):
    class LegacyExchange:
        def fetch_order_book(self, _symbol):
            return book("book-1")

        def close(self):
            return None

    order_id = add_order(sessions)
    worker = PaperExecutor(
        CONFIG,
        adapters=FakeAdapterPool({"fake": LegacyExchange()}),
        sessions=sessions,
        now=lambda: NOW,
    )

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "pending"
        assert order.rejection_reason == "instrument metadata is unavailable"
        assert order.retry_count == 1
        assert order.next_attempt_at.replace(tzinfo=timezone.utc) > NOW


def test_empty_instrument_metadata_is_deferred(sessions):
    order_id = add_order(sessions)
    worker = executor(sessions, MetadataSequenceExchange([[]]))

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "pending"
        assert order.rejection_reason == "instrument metadata is unavailable"
        assert order.retry_count == 1


def test_instrument_metadata_recovery_executes_deferred_order(sessions):
    order_id = add_order(sessions)
    exchange = MetadataSequenceExchange(
        [RuntimeError("temporary metadata failure"), [instrument_metadata()]],
        book("book-after-recovery", asks=[["100", "2"]]),
    )
    worker = executor(sessions, exchange)

    worker.process_once()
    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "pending"
        order.next_attempt_at = NOW
        session.commit()

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "filled"
        assert order.retry_count == 0


def test_partial_fill_state_survives_instrument_metadata_failure(sessions):
    order_id = add_order(
        sessions,
        quantity=Decimal("2"),
        status="partially_filled",
        filled_quantity=Decimal("1"),
    )
    worker = executor(sessions, MetadataSequenceExchange([RuntimeError("temporary failure")]))

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "partially_filled"
        assert order.filled_quantity == Decimal("1")
        assert order.retry_count == 1


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({**instrument_metadata(), "min_qty": None}, "instrument metadata is incomplete"),
        ({**instrument_metadata(), "qty_step": None}, "instrument metadata is incomplete"),
        (instrument_metadata(status="inactive"), "instrument is inactive"),
    ],
)
def test_structurally_invalid_instrument_metadata_is_rejected(sessions, metadata, reason):
    order_id = add_order(sessions)
    worker = executor(
        sessions,
        MetadataSequenceExchange([[metadata]], book("book-1", asks=[["100", "2"]])),
    )

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "rejected"
        assert order.rejection_reason == reason


def test_optional_min_notional_metadata_is_allowed_and_filled(sessions):
    # When min_notional is None (like Bybit Linear Perpetual), order should be executed
    metadata = {**instrument_metadata(), "min_notional": None}
    order_id = add_order(sessions)
    worker = executor(
        sessions,
        MetadataSequenceExchange([[metadata]], book("book-1", asks=[["100", "2"]])),
    )

    worker.process_once()

    with sessions() as session:
        order = session.get(Order, order_id)
        assert order.status == "filled"
        assert order.rejection_reason is None


def test_api_to_executor_to_query_flow(sessions, monkeypatch):
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
        lambda config: FakeAdapterPool({"fake": FakeExchange()}, config),
    )
    try:
        with TestClient(api.app) as client:
            rejected = client.post(
                "/api/v1/orders",
                json={
                    "request_id": "old-live-contract",
                    "mode": "live",
                    "exchange_id": "fake",
                    "symbol": "BTC/USD",
                    "side": "buy",
                    "order_type": "market",
                    "quantity": "1",
                },
            )
            assert rejected.status_code == 422

            response = client.post(
                "/api/v1/orders",
                json={
                    "request_id": "api-1",
                    "exchange_id": "fake",
                    "symbol": "BTC/USD",
                    "side": "buy",
                    "order_type": "market",
                    "quantity": "1",
                },
            )
            assert response.status_code == 202
            order_id = response.json()["id"]

            with sessions() as session:
                session.get(Order, order_id).next_attempt_at = NOW
                session.commit()

            worker = executor(sessions, FakeExchange(book("book-1", asks=[["100", "2"]])))
            worker.process_once()

            order_response = client.get(f"/api/v1/orders/{order_id}")
            fills_response = client.get(f"/api/v1/orders/{order_id}/fills")
            positions_response = client.get("/api/v1/positions")

        assert order_response.json()["status"] == "filled"
        assert "mode" not in order_response.json()
        assert fills_response.json()[0]["liquidity_role"] == "taker"
        assert positions_response.json()[0]["quantity"] == "1.000000000000000000"
    finally:
        api.app.dependency_overrides.clear()
