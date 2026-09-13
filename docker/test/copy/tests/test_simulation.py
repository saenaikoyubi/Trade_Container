from decimal import Decimal

from trade_common.simulation import simulate_order


def test_market_buy_walks_multiple_ask_levels():
    result = simulate_order(
        side="buy",
        order_type="market",
        quantity=Decimal("6"),
        limit_price=None,
        bids=[["99", "10"]],
        asks=[["100", "2"], ["101", "3"], ["102", "5"]],
        fee_rate=Decimal("0.001"),
    )

    assert result is not None
    assert result.fully_filled is True
    assert result.quantity == Decimal("6")
    assert result.average_price == Decimal("100.8333333333333333333333333")
    assert result.fee == Decimal("0.605")


def test_limit_buy_does_not_cross_prices_above_limit():
    result = simulate_order(
        side="buy",
        order_type="limit",
        quantity=Decimal("2"),
        limit_price=Decimal("100"),
        bids=[["99", "10"]],
        asks=[["101", "10"]],
        fee_rate=Decimal("0.001"),
    )

    assert result is None


def test_limit_sell_can_partially_fill_at_or_above_limit():
    result = simulate_order(
        side="sell",
        order_type="limit",
        quantity=Decimal("3"),
        limit_price=Decimal("100"),
        bids=[["101", "1.5"], ["100", "0.5"], ["99", "10"]],
        asks=[["102", "10"]],
        fee_rate=Decimal("0"),
    )

    assert result is not None
    assert result.quantity == Decimal("2.0")
    assert result.average_price == Decimal("100.75")
    assert result.fully_filled is False


def test_market_sell_walks_multiple_bid_levels():
    result = simulate_order(
        side="sell",
        order_type="market",
        quantity=Decimal("3"),
        limit_price=None,
        bids=[["101", "1"], ["100", "2"]],
        asks=[["102", "10"]],
        fee_rate=Decimal("0.001"),
    )

    assert result is not None
    assert result.quantity == Decimal("3")
    assert result.average_price == Decimal("100.3333333333333333333333333")
    assert result.fee == Decimal("0.301")


def test_rejects_invalid_order_input():
    try:
        simulate_order(
            side="hold",
            order_type="market",
            quantity=Decimal("1"),
            limit_price=None,
            bids=[],
            asks=[],
            fee_rate=Decimal("0"),
        )
    except ValueError as exc:
        assert str(exc) == "unsupported side: hold"
    else:
        raise AssertionError("ValueError was not raised")
