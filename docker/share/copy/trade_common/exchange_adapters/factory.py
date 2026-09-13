from __future__ import annotations

from ..config import ExchangeSettings
from .base import ExchangeAdapter
from .ccxt_adapter import CcxtAdapter


def create_exchange_adapter(config: ExchangeSettings) -> ExchangeAdapter:
    if config.adapter == "dydx":
        from .dydx_adapter import DydxAdapter

        return DydxAdapter(config)
    if config.adapter == "ccxt":
        return CcxtAdapter(config)
    raise RuntimeError(f"unsupported exchange adapter: {config.adapter}")
