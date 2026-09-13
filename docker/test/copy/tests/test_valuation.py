from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from trade_common.valuation import (
    ValuationError,
    market_assets,
    order_book_midpoint,
    position_side,
    unrealized_pnl,
)


def test_position_side_and_unrealized_pnl_cover_long_and_short():
    assert position_side(Decimal("1")) == "buy"
    assert position_side(Decimal("-1")) == "sell"
    assert unrealized_pnl(Decimal("1"), Decimal("100"), Decimal("110")) == Decimal("10")
    assert unrealized_pnl(Decimal("-1"), Decimal("100"), Decimal("90")) == Decimal("10")
    assert unrealized_pnl(Decimal("-1"), Decimal("100"), Decimal("110")) == Decimal("-10")


def test_market_assets_prefers_metadata_and_falls_back_to_symbol():
    assert market_assets({"base": "XBT", "quote": "USD"}, "BTC/USD") == ("XBT", "USD")
    assert market_assets({}, "BTC/USDT:USDT") == ("BTC", "USDT")
    assert market_assets(None, "ETH-USD") == ("ETH", "USD")


def test_order_book_midpoint_rejects_stale_and_empty_data():
    now = datetime.now(timezone.utc)
    price, observed_at = order_book_midpoint(
        {
            "bids": [["99", "1"]],
            "asks": [["101", "1"]],
            "_received_at": now,
            "_request_duration_seconds": 0.1,
        },
        now=now,
        max_age_seconds=10,
    )
    assert price == Decimal("100")
    assert observed_at == now

    with pytest.raises(ValuationError, match="stale"):
        order_book_midpoint(
            {"bids": [["99", "1"]], "asks": [["101", "1"]], "_received_at": now - timedelta(seconds=11)},
            now=now,
            max_age_seconds=10,
        )
    with pytest.raises(ValuationError, match="empty"):
        order_book_midpoint({"bids": [], "asks": [], "_received_at": now}, now=now, max_age_seconds=10)


def test_flat_position_has_no_side_and_invalid_average_is_rejected():
    with pytest.raises(ValuationError, match="flat"):
        position_side(Decimal("0"))
    with pytest.raises(ValuationError, match="average"):
        unrealized_pnl(Decimal("1"), Decimal("0"), Decimal("100"))
