from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from trade_common.config import ExchangeSettings, Settings
from trade_common.models import Base, ControlFlag, Order, Position
from trade_common.risk import evaluate_order


EXCHANGE = ExchangeSettings(
    exchange_id="fake",
    adapter="ccxt",
    symbols=("BTC/USD",),
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


def test_rejects_order_notional_over_limit():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        order = Order(
            request_id="risk-1",
            exchange_id="fake",
            symbol="BTC/USD",
            side="buy",
            order_type="market",
            quantity=Decimal("2"),
        )
        session.add(order)
        session.flush()
        decision = evaluate_order(session, order, Decimal("600"), CONFIG, EXCHANGE)
        assert decision.allowed is False
        assert decision.reason == "maximum order notional exceeded"


def test_allows_order_within_limits():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        order = Order(
            request_id="risk-2",
            exchange_id="fake",
            symbol="BTC/USD",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
        )
        session.add(order)
        session.flush()
        decision = evaluate_order(session, order, Decimal("500"), CONFIG, EXCHANGE)
        assert decision.allowed is True


def test_reduce_only_rejects_increase_and_reversal():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            Position(
                exchange_id="fake",
                symbol="BTC/USD",
                quantity=Decimal("1"),
            )
        )
        session.flush()
        increase = Order(
            request_id="reduce-increase",
            exchange_id="fake",
            symbol="BTC/USD",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
            reduce_only=True,
        )
        reversal = Order(
            request_id="reduce-reversal",
            exchange_id="fake",
            symbol="BTC/USD",
            side="sell",
            order_type="market",
            quantity=Decimal("2"),
            reduce_only=True,
        )
        session.add_all([increase, reversal])
        session.flush()

        assert evaluate_order(session, increase, Decimal("500"), CONFIG, EXCHANGE).reason == "reduce-only order would increase the position"
        assert evaluate_order(session, reversal, Decimal("500"), CONFIG, EXCHANGE).reason == "reduce-only order would reverse the position"


def test_close_only_rejects_opening_and_allows_full_reduce_beyond_normal_limits():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                ControlFlag(id=1, close_only=True, reason="manual close"),
                Position(
                    exchange_id="fake",
                    symbol="BTC/USD",
                    quantity=Decimal("3"),
                ),
            ]
        )
        session.flush()
        opening = Order(
            request_id="close-only-opening",
            exchange_id="fake",
            symbol="BTC/USD",
            side="buy",
            order_type="market",
            quantity=Decimal("1"),
        )
        closing = Order(
            request_id="close-only-closing",
            exchange_id="fake",
            symbol="BTC/USD",
            side="sell",
            order_type="market",
            quantity=Decimal("3"),
            reduce_only=True,
        )
        session.add_all([opening, closing])
        session.flush()

        opening_decision = evaluate_order(session, opening, Decimal("500"), CONFIG, EXCHANGE)
        closing_decision = evaluate_order(session, closing, Decimal("500"), CONFIG, EXCHANGE)

        assert opening_decision.allowed is False
        assert opening_decision.reason == "close-only mode is enabled: manual close"
        assert closing_decision.allowed is True


def test_kill_switch_rejects_reduce_only_close():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                ControlFlag(id=1, kill_switch=True, reason="halt"),
                Position(
                    exchange_id="fake",
                    symbol="BTC/USD",
                    quantity=Decimal("1"),
                ),
            ]
        )
        order = Order(
            request_id="halted-close",
            exchange_id="fake",
            symbol="BTC/USD",
            side="sell",
            order_type="market",
            quantity=Decimal("1"),
            reduce_only=True,
        )
        session.add(order)
        session.flush()

        decision = evaluate_order(session, order, Decimal("500"), CONFIG, EXCHANGE)

        assert decision.allowed is False
        assert decision.reason == "kill switch is enabled: halt"


def test_validate_instrument_quantity_with_and_without_min_notional():
    from trade_common.risk import validate_instrument_quantity

    # Bybit style: min_notional is None
    bybit_inst = {
        "status": "active",
        "min_qty": Decimal("0.001"),
        "qty_step": Decimal("0.001"),
        "max_qty": Decimal("1500.0"),
        "min_notional": None,
    }
    # valid quantity
    decision = validate_instrument_quantity(Decimal("0.010"), Decimal("76000"), bybit_inst)
    assert decision.allowed is True

    # step mismatch
    decision = validate_instrument_quantity(Decimal("0.0105"), Decimal("76000"), bybit_inst)
    assert decision.allowed is False
    assert decision.reason == "instrument quantity step is not aligned"

    # below min_qty
    decision = validate_instrument_quantity(Decimal("0.0005"), Decimal("76000"), bybit_inst)
    assert decision.allowed is False
    assert decision.reason == "minimum instrument quantity not met"

    # Binance style: min_notional is defined
    binance_inst = {
        "status": "active",
        "min_qty": Decimal("0.001"),
        "qty_step": Decimal("0.001"),
        "max_qty": Decimal("100.0"),
        "min_notional": Decimal("5.0"),
    }
    # valid
    decision = validate_instrument_quantity(Decimal("0.010"), Decimal("1000"), binance_inst)
    assert decision.allowed is True

    # below min_notional: 0.001 * 1000 = 1.0 < 5.0
    decision = validate_instrument_quantity(Decimal("0.001"), Decimal("1000"), binance_inst)
    assert decision.allowed is False
    assert decision.reason == "minimum instrument notional not met"


def test_validate_instrument_quantity_max_market_qty():
    from trade_common.risk import validate_instrument_quantity

    bybit_inst = {
        "status": "active",
        "min_qty": Decimal("0.001"),
        "qty_step": Decimal("0.001"),
        "max_qty": Decimal("1500.0"),
        "max_market_qty": Decimal("150.0"),
        "min_notional": Decimal("5.0"),
    }

    # market order within max_market_qty
    decision = validate_instrument_quantity(Decimal("150.0"), Decimal("50000"), bybit_inst, order_type="market")
    assert decision.allowed is True

    # market order exceeding max_market_qty but below max_qty
    decision = validate_instrument_quantity(Decimal("150.001"), Decimal("50000"), bybit_inst, order_type="market")
    assert decision.allowed is False
    assert decision.reason == "maximum instrument quantity exceeded"

    # limit order can exceed max_market_qty as long as within max_qty
    decision = validate_instrument_quantity(Decimal("200.0"), Decimal("50000"), bybit_inst, order_type="limit")
    assert decision.allowed is True

    # limit order exceeding max_qty is rejected
    decision = validate_instrument_quantity(Decimal("1501.0"), Decimal("50000"), bybit_inst, order_type="limit")
    assert decision.allowed is False
    assert decision.reason == "maximum instrument quantity exceeded"
