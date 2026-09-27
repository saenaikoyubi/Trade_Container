"""Checks that require PostgreSQL row locks and a real dump/restore cycle."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import Response
from sqlalchemy import create_engine, delete, event, select, text
from sqlalchemy.orm import sessionmaker

from trade_common.close_service import append_child
from trade_common.config import AccountSettings, ExchangeSettings, Settings
from trade_common.models import CloseRequest, CloseRequestPosition, ControlFlag, DailyPnl, Fill, Order, Position, RequestKey
from trade_common.runner import PaperExecutor
from trade_common.valuation import calculate_account_balance


@pytest.fixture(scope="module")
def postgres():
    dsn = os.getenv("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("isolated PostgreSQL test service is not configured")
    engine = create_engine(dsn, pool_pre_ping=True)
    with engine.begin() as connection:
        connection.execute(text("DROP SCHEMA public CASCADE"))
        connection.execute(text("CREATE SCHEMA public"))
    with tempfile.TemporaryDirectory() as scratch:
        password_file = Path(scratch) / "password"
        password_file.write_text("trade_test", encoding="utf-8")
        migration_env = {
            "DATABASE_PASSWORD_FILE": str(password_file),
            "POSTGRES_HOST": "postgres-test",
            "POSTGRES_DB": "trade_test",
            "POSTGRES_USER": "trade_test",
            "MIGRATION_DIR": "/app/migrations",
        }
        import db_migrate

        with patch.dict(os.environ, migration_env):
            db_migrate.main()
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM schema_migrations")) == 6
        yield engine, sessionmaker(bind=engine, expire_on_commit=False), password_file
    engine.dispose()


def test_kill_switch_commits_during_slow_metadata_fetch(postgres):
    import trade_api_service.main as api

    _engine, sessions, _password_file = postgres
    entered = threading.Event()
    release = threading.Event()
    exchange = ExchangeSettings(
        exchange_id="fake", adapter="ccxt", symbols=("BTC/USD",),
        taker_fee_rate=Decimal("0.001"), maker_fee_rate=Decimal("0.001"),
    )
    config = Settings(
        exchanges={"fake": exchange}, poll_interval_seconds=1,
        market_data_max_age_seconds=10,
        max_order_quantity=Decimal("10"), max_order_notional=Decimal("10000"),
        max_position_notional=Decimal("10000"), max_daily_loss=Decimal("10000"),
        max_price_deviation_pct=Decimal("0.5"), max_orders_per_minute=100,
    )

    class SlowAdapter:
        def fetch_instruments(self, _symbol):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test market data was not released")
            return [{"exchange_id": "fake", "symbol": "BTC/USD", "status": "active",
                     "min_qty": Decimal("1"), "qty_step": Decimal("1"), "max_qty": Decimal("10")}]

        def fetch_order_book(self, _symbol):
            raise AssertionError("kill switch must prevent fill")

    class Pool:
        def get(self, _exchange_id):
            return SlowAdapter()

    with sessions() as session:
        assert session.get(ControlFlag, 1) is not None
        order = Order(
            request_id="postgres-slow-order", exchange_id="fake", exchange_network="mainnet",
            symbol="BTC/USD", side="buy", order_type="market", quantity=Decimal("1"),
        )
        session.add(order)
        session.commit()
        order_id = order.id

    worker = PaperExecutor(config, adapters=Pool(), sessions=sessions)

    def enable_kill():
        with sessions() as session:
            return api.set_kill_switch(api.KillSwitchRequest(enabled=True, reason="test"), session, None)

    with ThreadPoolExecutor(max_workers=2) as threads:
        worker_future = threads.submit(worker.process_once)
        assert entered.wait(5)
        kill_future = threads.submit(enable_kill)
        try:
            result = kill_future.result(timeout=2)
            assert result.kill_switch is True
        finally:
            release.set()
        assert worker_future.result(timeout=5) is True

    with sessions() as session:
        assert session.get(Order, order_id).status == "canceled"
        assert session.scalars(select(Fill).where(Fill.order_id == order_id)).all() == []


def test_close_cancel_and_kill_switch_share_lock_order(postgres):
    import trade_api_service.main as api

    _engine, sessions, _password_file = postgres
    with sessions() as session:
        control = session.get(ControlFlag, 1)
        control.kill_switch = False
        control.reason = None
        position = Position(exchange_id="fake", symbol="ETH/USD", quantity=Decimal("1"))
        parent = CloseRequest(request_id="postgres-close", exchange_id="fake", status="queued")
        session.add_all([position, parent])
        session.flush()
        target = CloseRequestPosition(
            close_request_id=parent.id, position_id=position.id, symbol="ETH/USD",
            initial_position_quantity=Decimal("1"), remaining_position_quantity=Decimal("1"),
            status="queued",
        )
        session.add(target)
        session.flush()
        append_child(session, parent, target, quantity=Decimal("1"), network="mainnet")
        session.commit()

    gate = threading.Barrier(2)

    def cancel():
        gate.wait()
        with sessions() as session:
            return api.cancel_close_request("postgres-close", Response(), session, None)

    def kill():
        gate.wait()
        with sessions() as session:
            return api.set_kill_switch(api.KillSwitchRequest(enabled=True, reason="test"), session, None)

    with ThreadPoolExecutor(max_workers=2) as threads:
        cancel_future = threads.submit(cancel)
        kill_future = threads.submit(kill)
        assert cancel_future.result(timeout=5).status in {"canceling", "canceled"}
        assert kill_future.result(timeout=5).kill_switch is True

    with sessions() as session:
        parent = session.scalar(select(CloseRequest).where(CloseRequest.request_id == "postgres-close"))
        assert parent.status in {"canceling", "canceled"}
        assert session.scalar(select(Order).where(Order.close_request_id == parent.id)).cancellation_requested


def test_kill_switch_does_not_wait_for_eager_child_preparation(postgres, monkeypatch):
    import trade_api_service.main as api

    _engine, sessions, _password_file = postgres
    exchange = ExchangeSettings(
        exchange_id="bybit", adapter="ccxt", symbols=("XRPUSDT",),
        taker_fee_rate=Decimal("0.001"), maker_fee_rate=Decimal("0.001"),
    )
    config = Settings(
        exchanges={"bybit": exchange}, poll_interval_seconds=1,
        market_data_max_age_seconds=10, max_order_quantity=Decimal("100"),
        max_order_notional=Decimal("100000"), max_position_notional=Decimal("100000"),
        max_daily_loss=Decimal("10000"), max_price_deviation_pct=Decimal("0.5"),
        max_orders_per_minute=100,
    )

    class Adapter:
        def resolve_symbol(self, symbol):
            assert symbol == "XRPUSDT"
            return symbol

        def fetch_instruments(self, _symbol):
            return [{
                "exchange_id": "bybit", "symbol": "XRPUSDT", "status": "active",
                "contract_type": "linear_perpetual", "quote_asset": "USDT", "settle_asset": "USDT",
                "max_market_qty": Decimal("1"), "max_qty": Decimal("100"),
            }]

        def fetch_order_book(self, _symbol):
            return {"bids": [["99", "100"]], "asks": [["101", "100"]],
                    "_received_at": datetime.now(timezone.utc)}

    class Pool:
        def get(self, _exchange_id):
            return Adapter()

    monkeypatch.setattr(api, "_runtime_settings", lambda: config)
    monkeypatch.setattr(api, "_runtime_pool", lambda _config: Pool())
    entered = threading.Event()
    release = threading.Event()
    original = api.append_initial_children

    def slow_preparation(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(api, "append_initial_children", slow_preparation)
    with sessions() as session:
        control = session.get(ControlFlag, 1)
        control.kill_switch = False
        control.reason = None
        session.add(Position(exchange_id="bybit", symbol="XRPUSDT", quantity=Decimal("51")))
        session.commit()

    def create_close():
        with sessions() as session:
            return api.close_positions(api.PositionCloseRequest(
                request_id="slow-eager-close", exchange_id="bybit", symbol="XRPUSDT",
            ), Response(), session, None)

    def enable_kill():
        with sessions() as session:
            return api.set_kill_switch(api.KillSwitchRequest(enabled=True, reason="test"), session, None)

    with ThreadPoolExecutor(max_workers=2) as threads:
        close_future = threads.submit(create_close)
        assert entered.wait(5)
        kill_future = threads.submit(enable_kill)
        try:
            assert kill_future.result(timeout=2).kill_switch is True
        finally:
            release.set()
        with pytest.raises(api.HTTPException) as rejected:
            close_future.result(timeout=5)
        assert rejected.value.status_code == 409

    with sessions() as session:
        assert session.scalar(select(CloseRequest).where(CloseRequest.request_id == "slow-eager-close")) is None
        session.get(ControlFlag, 1).kill_switch = False
        session.commit()


def test_kill_switch_finishes_waiting_close_without_active_children(postgres):
    import trade_api_service.main as api

    _engine, sessions, _password_file = postgres
    with sessions() as session:
        position = Position(exchange_id="fake", symbol="WAIT/USD", quantity=Decimal("1"))
        parent = CloseRequest(request_id="waiting-close-kill", exchange_id="fake", status="waiting")
        session.add_all([position, parent])
        session.flush()
        target = CloseRequestPosition(
            close_request_id=parent.id, position_id=position.id, symbol="WAIT/USD",
            initial_position_quantity=Decimal("1"), remaining_position_quantity=Decimal("1"),
            status="waiting",
        )
        session.add(target)
        session.commit()
        parent_id, target_id = parent.id, target.id

    with sessions() as session:
        result = api.set_kill_switch(api.KillSwitchRequest(enabled=True, reason="test"), session, None)
        assert result.kill_switch is True

    with sessions() as session:
        assert session.get(CloseRequest, parent_id).status == "canceled"
        assert session.get(CloseRequestPosition, target_id).status == "canceled"
        session.get(ControlFlag, 1).kill_switch = False
        session.commit()


def test_kill_switch_commits_with_ten_thousand_close_children(postgres):
    import trade_api_service.main as api

    _engine, sessions, _password_file = postgres
    with sessions() as session:
        position = Position(exchange_id="fake", symbol="BULK/USD", quantity=Decimal("10000"))
        parent = CloseRequest(request_id="bulk-close-kill", exchange_id="fake", status="queued")
        session.add_all([position, parent])
        session.flush()
        target = CloseRequestPosition(
            close_request_id=parent.id, position_id=position.id, symbol="BULK/USD",
            initial_position_quantity=Decimal("10000"), remaining_position_quantity=Decimal("10000"),
            status="queued",
        )
        session.add(target)
        session.flush()
        parent_id, target_id = parent.id, target.id
        session.execute(text("""
            INSERT INTO orders (
                id, request_id, exchange_id, exchange_network, symbol, side,
                order_type, quantity, reduce_only, close_request_id, close_position_id, close_sequence
            )
            SELECT md5('bulk-close-child-' || n), 'bulk-close-child-' || n,
                   'fake', 'mainnet', 'BULK/USD', 'sell', 'market', 1, TRUE,
                   :parent_id, :target_id, n
            FROM generate_series(1, 10000) AS n
        """), {"parent_id": parent_id, "target_id": target_id})
        session.commit()

    started = time.perf_counter()
    with sessions() as session:
        result = api.set_kill_switch(api.KillSwitchRequest(enabled=True, reason="test"), session, None)
    elapsed = time.perf_counter() - started
    assert result.kill_switch is True
    assert result.cancellation_requested_count >= 10000
    assert elapsed < 5, f"kill switch took {elapsed:.2f}s for 10,000 children"

    with sessions() as session:
        assert session.get(CloseRequest, parent_id).status == "canceling"
        assert session.scalar(select(Order).where(Order.close_position_id == target_id)).cancellation_requested
        session.execute(delete(Order).where(Order.close_position_id == target_id))
        session.execute(delete(CloseRequestPosition).where(CloseRequestPosition.id == target_id))
        session.execute(delete(CloseRequest).where(CloseRequest.id == parent_id))
        session.execute(delete(Position).where(Position.id == position.id))
        session.get(ControlFlag, 1).kill_switch = False
        session.commit()


def test_balance_ledger_components_share_one_committed_snapshot(postgres):
    engine, sessions, _password_file = postgres
    exchange = ExchangeSettings(
        exchange_id="snapshot", adapter="ccxt", symbols=("BTC/USD",),
        taker_fee_rate=Decimal("0"), maker_fee_rate=Decimal("0"),
    )
    config = Settings(
        exchanges={"snapshot": exchange}, poll_interval_seconds=1,
        market_data_max_age_seconds=10, max_order_quantity=Decimal("10"),
        max_order_notional=Decimal("10000"), max_position_notional=Decimal("10000"),
        max_daily_loss=Decimal("10000"), max_price_deviation_pct=Decimal("0.5"),
        max_orders_per_minute=100,
        account=AccountSettings(initial_balance=Decimal("10000"), default_leverage=Decimal("10")),
    )

    class Adapter:
        def fetch_instruments(self, _symbol):
            return [{"settle_asset": "USD"}]

        def fetch_prices(self, _symbols):
            return [{"mid_price": Decimal("110")}]

    class Pool:
        def get(self, _exchange_id):
            return Adapter()

    with sessions() as session:
        for existing in session.scalars(select(Position)).all():
            existing.quantity = Decimal("0")
            existing.average_entry_price = Decimal("0")
        session.execute(delete(DailyPnl))
        session.add(Position(exchange_id="snapshot", symbol="BTC/USD",
                             quantity=Decimal("1"), average_entry_price=Decimal("100")))
        session.commit()

    switched = False

    def change_ledger_after_read(_connection, _cursor, statement, _parameters, _context, _executemany):
        nonlocal switched
        if switched or "positions" not in statement.lower() or "daily_pnl" not in statement.lower():
            return
        switched = True
        with sessions() as writer:
            position = writer.scalar(select(Position).where(Position.exchange_id == "snapshot"))
            position.quantity = Decimal("0")
            writer.add(DailyPnl(trade_date=datetime.now(timezone.utc).date(), realized_pnl=Decimal("10")))
            writer.commit()

    event.listen(engine, "after_cursor_execute", change_ledger_after_read)
    try:
        with sessions() as session:
            balance = calculate_account_balance(session, config, Pool())
    finally:
        event.remove(engine, "after_cursor_execute", change_ledger_after_read)
    assert switched
    assert balance.realized_pnl == Decimal("0")
    assert balance.unrealized_pnl == Decimal("10")


def test_each_backup_restores_and_verifies_all_public_tables(postgres):
    engine, sessions, password_file = postgres
    with sessions() as session:
        position = Position(exchange_id="fake", symbol="SOL/USD", quantity=Decimal("2"))
        parent = CloseRequest(request_id="backup-parent", exchange_id="fake", status="queued")
        session.add_all([position, parent])
        session.flush()
        target = CloseRequestPosition(
            close_request_id=parent.id, position_id=position.id, symbol="SOL/USD",
            initial_position_quantity=Decimal("2"), remaining_position_quantity=Decimal("2"),
            status="queued",
        )
        session.add(target)
        session.flush()
        child = append_child(session, parent, target, quantity=Decimal("2"), network="mainnet")
        session.add(RequestKey(request_id=parent.request_id, operation_kind="close_request", target_id=parent.id))
        session.commit()

    with tempfile.TemporaryDirectory() as scratch:
        source_env = {**os.environ, "BACKUP_DIR": scratch,
                      "DATABASE_PASSWORD_FILE": str(password_file), "POSTGRES_HOST": "postgres-test",
                      "POSTGRES_DB": "trade_test", "POSTGRES_USER": "trade_test"}
        names = []
        for _ in range(2):
            backup = subprocess.run(["sh", "/app/db_scripts/backup.sh"], env=source_env,
                                    text=True, capture_output=True, timeout=30)
            assert backup.returncode == 0, backup.stdout + backup.stderr
            dumps = {path.name for path in Path(scratch).glob("*.dump")}
            new_names = dumps - set(names)
            assert len(new_names) == 1
            name = new_names.pop()
            names.append(name)
            metadata = (Path(scratch) / name.replace(".dump", ".metadata")).read_text()
            assert "format=trade-container-backup-v2" in metadata
            assert "count_request_keys=" in metadata
            assert "count_close_requests=" in metadata
            assert "count_close_request_positions=" in metadata

        assert names[0] != names[1]
        restore_engine = create_engine(
            "postgresql+psycopg://trade_test:trade_test@postgres-restore:5432/trade_restore"
        )
        for name in names:
            with restore_engine.begin() as connection:
                connection.execute(text("DROP SCHEMA public CASCADE"))
                connection.execute(text("CREATE SCHEMA public"))
            restore_env = {**os.environ, "BACKUP_DIR": scratch,
                           "DATABASE_PASSWORD_FILE": str(password_file),
                           "POSTGRES_HOST": "postgres-restore", "POSTGRES_DB": "trade_restore",
                           "POSTGRES_USER": "trade_test", "SOURCE_DATABASE_NAME": "trade_test",
                           "RESTORE_CONFIRM": "isolated", "BACKUP_FILE": name}
            restored = subprocess.run(["sh", "/app/db_scripts/restore.sh"], env=restore_env,
                                      text=True, capture_output=True, timeout=30)
            assert restored.returncode == 0, restored.stdout + restored.stderr
            assert "restore verification passed" in restored.stdout
            with restore_engine.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM request_keys")) >= 2
                assert connection.scalar(text("SELECT count(*) FROM close_requests")) >= 1
        restore_engine.dispose()
