from __future__ import annotations

from collections.abc import Callable
from threading import RLock

from ..config import ExchangeSettings, Settings
from .base import ExchangeAdapter
from .factory import create_exchange_adapter


class ExchangeAdapterPool:
    def __init__(
        self,
        config: Settings,
        *,
        factory: Callable[[ExchangeSettings], ExchangeAdapter] = create_exchange_adapter,
    ):
        self.config = config
        self.factory = factory
        self._adapters: dict[str, ExchangeAdapter] = {}
        self._lock = RLock()

    def get(self, exchange_id: str) -> ExchangeAdapter:
        exchange_config = self.config.exchange(exchange_id)
        if exchange_config is None:
            raise RuntimeError(f"exchange is not configured: {exchange_id}")
        with self._lock:
            adapter = self._adapters.get(exchange_id)
            if adapter is None:
                adapter = self.factory(exchange_config)
                self._adapters[exchange_id] = adapter
            return adapter

    def get_existing(self, exchange_id: str) -> ExchangeAdapter | None:
        with self._lock:
            return self._adapters.get(exchange_id)

    def close(self) -> None:
        first_error: Exception | None = None
        with self._lock:
            adapters = list(self._adapters.values())
            self._adapters.clear()
        for adapter in adapters:
            try:
                adapter.close()
            except Exception as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error
