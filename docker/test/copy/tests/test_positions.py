from decimal import Decimal

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from trade_common.models import Base, Order, Position
from trade_common.repository import record_fill


def test_round_trip_updates_realized_pnl_and_fees():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        buy = Order(
            request_id="buy-1",
            exchange_id="fake",
            symbol="BTC/USD",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
        )
        session.add(buy)
        session.flush()
        record_fill(
            session,
            buy,
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("1"),
        )

        sell = Order(
            request_id="sell-1",
            exchange_id="fake",
            symbol="BTC/USD",
            side="sell",
            order_type="market",
            quantity=Decimal("1"),
        )
        session.add(sell)
        session.flush()
        record_fill(
            session,
            sell,
            quantity=Decimal("1"),
            price=Decimal("110"),
            fee=Decimal("1"),
        )
        session.commit()

        position = session.scalar(select(Position))
        assert position is not None
        assert position.quantity == Decimal("0")
        assert position.average_entry_price == Decimal("0")
        assert position.realized_pnl == Decimal("8")
