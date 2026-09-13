from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any


def read_secret(path: str | None, *, required: bool = True) -> str | None:
    if not path:
        if required:
            raise RuntimeError("secret file path is not configured")
        return None
    secret_path = Path(path)
    if not secret_path.is_file():
        if required:
            raise RuntimeError(f"secret file does not exist: {secret_path}")
        return None
    value = secret_path.read_text(encoding="utf-8").strip().lstrip("\ufeff")
    if not value and required:
        raise RuntimeError(f"secret file is empty: {secret_path}")
    return value or None


def load_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def decimal_value(value: Any) -> Decimal:
    return Decimal(str(value))


@dataclass(frozen=True)
class ExchangeSettings:
    exchange_id: str
    adapter: str
    symbols: tuple[str, ...]
    taker_fee_rate: Decimal
    maker_fee_rate: Decimal
    options: dict[str, Any] = field(default_factory=dict)
    market_data_max_age_seconds: float = 10.0


@dataclass(frozen=True)
class AccountSettings:
    currency: str = "USDT"
    initial_balance: Decimal = Decimal("10000.0")
    default_leverage: Decimal = Decimal("10.0")


@dataclass(frozen=True)
class Settings:
    exchanges: dict[str, ExchangeSettings]
    poll_interval_seconds: float
    market_data_max_age_seconds: float
    max_order_quantity: Decimal
    max_order_notional: Decimal
    max_position_notional: Decimal
    max_daily_loss: Decimal
    max_price_deviation_pct: Decimal
    max_orders_per_minute: int
    exchange_network: str = "mainnet"
    account: AccountSettings = field(default_factory=AccountSettings)

    def exchange(self, exchange_id: str) -> ExchangeSettings | None:
        return self.exchanges.get(exchange_id)

    @classmethod
    def from_file(cls, path: str | Path) -> "Settings":
        raw = load_json(path)
        risk = raw["risk"]
        market_data_max_age_seconds = float(raw.get("market_data_max_age_seconds", 10.0))
        if market_data_max_age_seconds <= 0:
            raise ValueError("market_data_max_age_seconds must be positive")
        raw_exchanges = raw.get("exchanges")
        if not isinstance(raw_exchanges, dict) or not raw_exchanges:
            raise ValueError("exchanges must be a non-empty object")

        exchanges: dict[str, ExchangeSettings] = {}
        for exchange_id, exchange_raw in raw_exchanges.items():
            if not isinstance(exchange_id, str) or not exchange_id or len(exchange_id) > 32:
                raise ValueError("exchange id must be between 1 and 32 characters")
            if not isinstance(exchange_raw, dict):
                raise ValueError(f"exchange configuration must be an object: {exchange_id}")
            adapter = exchange_raw.get("adapter")
            if adapter not in {"ccxt", "dydx"}:
                raise ValueError(f"unsupported exchange adapter: {adapter}")
            if adapter == "dydx" and exchange_id != "dydx":
                raise ValueError("the dydx adapter must use exchange id 'dydx'")
            symbols = exchange_raw.get("symbols")
            if not isinstance(symbols, list) or not symbols or any(not isinstance(item, str) or not item for item in symbols):
                raise ValueError(f"exchange symbols must be a non-empty string list: {exchange_id}")
            if any(item != item.strip() for item in symbols) or len(set(symbols)) != len(symbols):
                raise ValueError(f"exchange symbols must be unique and contain no surrounding whitespace: {exchange_id}")
            options = exchange_raw.get("options", {})
            if not isinstance(options, dict):
                raise ValueError(f"exchange options must be an object: {exchange_id}")
            fees = exchange_raw.get("fees")
            if not isinstance(fees, dict):
                raise ValueError(f"exchange fees must be configured: {exchange_id}")
            maker_fee_rate = decimal_value(fees["maker"])
            taker_fee_rate = decimal_value(fees["taker"])
            if maker_fee_rate < 0 or taker_fee_rate < 0:
                raise ValueError(f"exchange fees must not be negative: {exchange_id}")
            exchanges[exchange_id] = ExchangeSettings(
                exchange_id=exchange_id,
                adapter=adapter,
                symbols=tuple(symbols),
                taker_fee_rate=taker_fee_rate,
                maker_fee_rate=maker_fee_rate,
                options=dict(options),
                market_data_max_age_seconds=market_data_max_age_seconds,
            )

        account_raw = raw.get("account", {})
        if not isinstance(account_raw, dict):
            raise ValueError("account must be an object")
        currency = str(account_raw.get("currency", "USDT"))
        initial_balance = decimal_value(account_raw.get("initial_balance", "10000.0"))
        default_leverage = decimal_value(account_raw.get("default_leverage", "10.0"))
        if currency not in {"USD", "USDC", "USDT"}:
            raise ValueError("account currency must be one of USD, USDC, or USDT")
        if initial_balance < 0:
            raise ValueError("account initial_balance must not be negative")
        if default_leverage <= 0:
            raise ValueError("account default_leverage must be positive")

        return cls(
            exchanges=exchanges,
            poll_interval_seconds=float(raw.get("poll_interval_seconds", 1.0)),
            market_data_max_age_seconds=market_data_max_age_seconds,
            max_order_quantity=decimal_value(risk["max_order_quantity"]),
            max_order_notional=decimal_value(risk["max_order_notional"]),
            max_position_notional=decimal_value(risk["max_position_notional"]),
            max_daily_loss=decimal_value(risk["max_daily_loss"]),
            max_price_deviation_pct=decimal_value(risk["max_price_deviation_pct"]),
            max_orders_per_minute=int(risk["max_orders_per_minute"]),
            exchange_network="mainnet",
            account=AccountSettings(
                currency=currency,
                initial_balance=initial_balance,
                default_leverage=default_leverage,
            ),
        )


def settings() -> Settings:
    return Settings.from_file(os.getenv("TRADE_CONFIG_FILE", "/app/config/settings.json"))
