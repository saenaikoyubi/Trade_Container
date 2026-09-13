"""Backward-compatible imports for public exchange market data."""

from .exchange_adapters.ccxt_adapter import CcxtAdapter
from .exchange_adapters.common import normalize_order_book

ExchangeClient = CcxtAdapter

__all__ = ["ExchangeClient", "normalize_order_book"]
