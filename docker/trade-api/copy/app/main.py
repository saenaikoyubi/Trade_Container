from __future__ import annotations

import base64
import binascii
import asyncio
import hashlib
import json
import hmac
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from trade_common.config import read_secret, settings
from trade_common.database import engine, session_factory
from trade_common.exchange_adapters import ExchangeAdapterPool
from trade_common.logging_config import configure_logging
from trade_common.models import ControlFlag, DailyPnl, Fill, Order, Position
from trade_common.risk import validate_instrument_quantity
from trade_common.valuation import (
    ValuationError,
    calculate_account_balance,
    market_assets,
    order_book_midpoint,
    position_side,
    unrealized_pnl,
)


configure_logging("trade-api")
LOGGER = logging.getLogger(__name__)
NON_TERMINAL_ORDER_STATUSES = ("pending", "processing", "open", "partially_filled")
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
METADATA_RETRY_DELAYS = (1, 2, 4, 8, 16, 32, 60)
METADATA_REFRESH_INTERVAL_SECONDS = 60 * 60


async def _warm_bybit_once(application: FastAPI) -> bool:
    pool = getattr(application.state, "adapter_pool", None)
    if pool is None:
        return True
    try:
        adapter = await asyncio.to_thread(pool.get, "bybit")
        await asyncio.to_thread(adapter.fetch_instruments)
    except Exception as exc:
        application.state.bybit_metadata = {"ready": False, "last_error": str(exc)}
        LOGGER.warning("Bybit metadata warmup failed", exc_info=True)
        return False
    metadata_ready = bool(getattr(adapter, "metadata_ready", True))
    last_error = getattr(adapter, "metadata_last_error", None)
    application.state.bybit_metadata = {"ready": metadata_ready, "last_error": last_error}
    if last_error:
        LOGGER.warning(
            "Bybit metadata refresh failed; cache readiness=%s error=%s",
            metadata_ready,
            last_error,
        )
    return metadata_ready and last_error is None


async def _maintain_bybit_metadata(application: FastAPI, initially_fresh: bool) -> None:
    fresh = initially_fresh
    attempt = 0 if initially_fresh else 1
    while True:
        delay = (
            METADATA_REFRESH_INTERVAL_SECONDS
            if fresh
            else METADATA_RETRY_DELAYS[min(attempt - 1, len(METADATA_RETRY_DELAYS) - 1)]
        )
        await asyncio.sleep(delay)
        fresh = await _warm_bybit_once(application)
        attempt = 0 if fresh else attempt + 1


@asynccontextmanager
async def lifespan(application: FastAPI):
    runtime_config = None
    pool = None
    retry_task = None
    config_error = None
    try:
        runtime_config = settings()
        pool = ExchangeAdapterPool(runtime_config)
    except Exception as exc:
        config_error = str(exc)
        LOGGER.warning("runtime configuration is unavailable during startup", exc_info=True)
    application.state.runtime_config = runtime_config
    application.state.config_error = config_error
    application.state.adapter_pool = pool
    application.state.bybit_metadata = {
        "ready": runtime_config is not None and "bybit" not in runtime_config.exchanges,
        "last_error": None,
    }
    if runtime_config is not None and "bybit" in runtime_config.exchanges:
        initially_fresh = await _warm_bybit_once(application)
        retry_task = asyncio.create_task(_maintain_bybit_metadata(application, initially_fresh))
    application.state.metadata_retry_task = retry_task
    try:
        yield
    finally:
        if retry_task is not None:
            retry_task.cancel()
            try:
                await retry_task
            except asyncio.CancelledError:
                pass
        if pool is not None:
            try:
                await asyncio.to_thread(pool.close)
            except Exception:
                LOGGER.warning("API adapter pool close failed", exc_info=True)


app = FastAPI(title="Trade Container API", version="1.0.0", lifespan=lifespan)


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128, pattern=REQUEST_ID_PATTERN)
    exchange_id: str = Field(min_length=1, max_length=32)
    symbol: str = Field(min_length=1, max_length=64)
    side: Literal["buy", "sell"]
    order_type: Literal["market", "limit"]
    quantity: Decimal = Field(gt=0)
    limit_price: Decimal | None = Field(default=None, gt=0)
    reduce_only: bool = False
    strategy_id: str | None = Field(default=None, min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_limit_price(self):
        if self.order_type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required for limit orders")
        if self.order_type == "market" and self.limit_price is not None:
            raise ValueError("limit_price must be omitted for market orders")
        return self


class OrderView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    request_id: str
    strategy_id: str | None
    exchange_id: str
    exchange_network: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    limit_price: Decimal | None
    status: str
    rejection_reason: str | None
    reduce_only: bool
    filled_quantity: Decimal
    average_fill_price: Decimal | None
    total_fee: Decimal
    cancellation_requested: bool
    resting_since: datetime | None
    last_market_data_id: str | None
    retry_count: int
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime


class FillView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    order_id: str
    exchange_id: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    liquidity_role: str
    market_data_id: str | None
    executed_at: datetime


class PositionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    exchange_id: str
    symbol: str
    quantity: Decimal
    average_entry_price: Decimal
    realized_pnl: Decimal
    updated_at: datetime


class CurrentPositionView(BaseModel):
    exchange_id: str
    symbol: str
    base_asset: str
    quote_asset: str
    position_side: Literal["buy", "sell"]
    quantity: Decimal
    average_entry_price: Decimal
    current_price: Decimal | None
    unrealized_pnl: Decimal | None
    position_updated_at: datetime
    price_observed_at: datetime | None
    valuation_status: Literal["ok", "unavailable"]
    valuation_error: str | None = None


class CurrentPositionsView(BaseModel):
    exchange_id: str
    refreshed_at: datetime
    valuation_complete: bool
    unpriced_count: int
    total_unrealized_pnl: Decimal | None
    positions: list[CurrentPositionView]


class InstrumentView(BaseModel):
    exchange_id: str
    symbol: str
    base_asset: str | None
    quote_asset: str | None
    settle_asset: str | None
    contract_type: str
    contract_size: Decimal | None
    qty_step: Decimal | None
    min_qty: Decimal | None
    max_qty: Decimal | None
    max_market_qty: Decimal | None = None
    price_step: Decimal | None
    min_notional: Decimal | None
    quantity_unit: str | None = None
    status: str


class PriceView(BaseModel):
    exchange_id: str
    symbol: str
    mark_price: Decimal | None
    last_price: Decimal | None
    bid_price: Decimal | None
    ask_price: Decimal | None
    mid_price: Decimal
    observed_at: datetime


class BalanceView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    currency: str
    initial_balance: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_fee: Decimal
    equity: Decimal
    used_margin: Decimal
    available_balance: Decimal
    updated_at: datetime


class OrderHistoryPage(BaseModel):
    items: list[OrderView]
    next_cursor: str | None


class FillHistoryPage(BaseModel):
    items: list[FillView]
    next_cursor: str | None


class PnlHistoryPoint(BaseModel):
    trade_date: date
    daily_realized_pnl: Decimal
    cumulative_realized_pnl: Decimal


class PnlHistoryView(BaseModel):
    timezone: Literal["UTC"] = "UTC"
    from_date: date | None
    to_date: date | None
    points: list[PnlHistoryPoint]


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    reason: str | None = Field(default=None, max_length=500)


class PositionCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=128, pattern=REQUEST_ID_PATTERN)
    exchange_id: str = Field(min_length=1, max_length=32)
    symbol: str | None = Field(default=None, min_length=1, max_length=64)
    strategy_id: str | None = Field(default=None, min_length=1, max_length=64)


class ExchangeView(BaseModel):
    exchange_id: str
    adapter: Literal["ccxt", "dydx"]
    network: Literal["mainnet"] = "mainnet"
    symbols: list[str]


class TradingControlView(BaseModel):
    kill_switch: bool
    close_only: bool
    reason: str | None
    updated_at: datetime
    cancellation_requested_count: int = 0


class CloseOrdersView(BaseModel):
    items: list[OrderView]


def get_session():
    session = session_factory()()
    try:
        yield session
    finally:
        session.close()


def authenticate(authorization: Annotated[str | None, Header()] = None) -> None:
    expected = read_secret(os.getenv("API_TOKEN_FILE", "/run/secrets/api_token"))
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token is required")
    supplied = authorization.removeprefix("Bearer ")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="invalid Bearer token")


Auth = Annotated[None, Depends(authenticate)]
Db = Annotated[Session, Depends(get_session)]


def _runtime_settings():
    runtime_config = getattr(app.state, "runtime_config", None)
    return runtime_config if runtime_config is not None else settings()


def _runtime_pool(config) -> ExchangeAdapterPool:
    pool = getattr(app.state, "adapter_pool", None)
    if pool is None or pool.config is not config:
        pool = ExchangeAdapterPool(config)
        app.state.adapter_pool = pool
    return pool


def _existing_adapter(pool, exchange_id: str):
    getter = getattr(pool, "get_existing", None)
    if callable(getter):
        return getter(exchange_id)
    return getattr(pool, "_adapters", {}).get(exchange_id)


def _exchange_config(config, exchange_id: str):
    exchange = config.exchange(exchange_id)
    if exchange is None:
        raise HTTPException(status_code=422, detail="exchange is not allowed")
    return exchange


def _canonical_symbol(config, exchange, raw_symbol: str) -> str:
    if raw_symbol != raw_symbol.strip():
        raise HTTPException(status_code=422, detail="symbol must not contain surrounding whitespace")
    if raw_symbol in exchange.symbols:
        return raw_symbol
    try:
        return _runtime_pool(config).get(exchange.exchange_id).resolve_symbol(raw_symbol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="symbol is not allowed for exchange") from exc
    except RuntimeError as exc:
        if "unsupported CCXT exchange" in str(exc):
            raise HTTPException(status_code=422, detail="symbol is not allowed for exchange") from exc
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable") from exc


def _cached_history_canonical(config, exchange, raw_symbol: str) -> str | None:
    if raw_symbol in exchange.symbols:
        return raw_symbol
    pool = getattr(app.state, "adapter_pool", None)
    if pool is None or getattr(pool, "config", None) != config:
        return None
    adapter = _existing_adapter(pool, exchange.exchange_id)
    resolver = getattr(adapter, "resolve_cached_symbol", None) if adapter is not None else None
    if not callable(resolver):
        return None
    try:
        canonical = resolver(raw_symbol)
    except Exception:
        LOGGER.debug(
            "cached history symbol resolution failed: exchange=%s symbol=%s",
            exchange.exchange_id,
            raw_symbol,
            exc_info=True,
        )
        return None
    return canonical if canonical in exchange.symbols else None


def _history_symbol_condition(model, exchange_id: str | None, raw_symbol: str):
    if raw_symbol != raw_symbol.strip():
        raise HTTPException(status_code=422, detail="symbol must not contain surrounding whitespace")
    try:
        config = _runtime_settings()
    except Exception:
        return model.symbol == raw_symbol
    if exchange_id is not None:
        exchange = config.exchange(exchange_id)
        if exchange is None:
            return model.symbol == raw_symbol
        canonical = _cached_history_canonical(config, exchange, raw_symbol)
        if canonical is None or canonical == raw_symbol:
            return model.symbol == raw_symbol
        return model.symbol.in_((raw_symbol, canonical))
    matches = [model.symbol == raw_symbol]
    for exchange in config.exchanges.values():
        canonical = _cached_history_canonical(config, exchange, raw_symbol)
        if canonical is not None and canonical != raw_symbol:
            matches.append(and_(model.exchange_id == exchange.exchange_id, model.symbol == canonical))
    return or_(*matches)


def _validate_date_range(from_date: date | None, to_date: date | None) -> None:
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if from_date and to_date and (to_date - from_date).days > 3660:
        raise HTTPException(status_code=422, detail="date range must not exceed 3660 days")


def _date_bounds(from_date: date | None, to_date: date | None) -> tuple[datetime | None, datetime | None]:
    _validate_date_range(from_date, to_date)
    start = datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc) if from_date else None
    end = datetime.combine(to_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) if to_date else None
    return start, end


def _encode_cursor(timestamp: datetime, item_id: str) -> str:
    payload = json.dumps({"timestamp": timestamp.isoformat(), "id": item_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(cursor + padding).decode("utf-8"))
        timestamp = datetime.fromisoformat(payload["timestamp"])
        item_id = str(payload["id"])
        if not item_id:
            raise ValueError("empty id")
    except (binascii.Error, KeyError, TypeError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="invalid cursor") from exc
    return timestamp, item_id


@app.get("/health")
def health():
    return {
        "status": "ok",
        "environment": "paper",
        "simulation": True,
        "timestamp": datetime.now(timezone.utc),
    }


@app.get("/ready")
def ready(db: Db):
    database_ready = True
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        database_ready = False
    runtime_config = getattr(app.state, "runtime_config", None)
    config_ready = runtime_config is not None
    config_error = getattr(app.state, "config_error", None)
    bybit_required = runtime_config is not None and "bybit" in runtime_config.exchanges
    metadata_state = getattr(app.state, "bybit_metadata", {"ready": not bybit_required, "last_error": None})
    bybit_ready = bool(metadata_state.get("ready")) if bybit_required else True
    pool = getattr(app.state, "adapter_pool", None)
    if bybit_ready and bybit_required and pool is not None:
        adapter = _existing_adapter(pool, "bybit")
        bybit_ready = bool(adapter is not None and getattr(adapter, "metadata_ready", False))
        if adapter is not None:
            metadata_state["last_error"] = getattr(adapter, "metadata_last_error", None)
    is_ready = database_ready and config_ready and bybit_ready
    payload = {
        "status": "ready" if is_ready else "not_ready",
        "environment": "paper",
        "simulation": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": {
            "database": {"ready": database_ready},
            "configuration": {"ready": config_ready, "last_error": config_error},
            "bybit_metadata": {
                "required": bybit_required,
                "ready": bybit_ready,
                "last_error": metadata_state.get("last_error"),
            },
        },
    }
    if not is_ready:
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/api/v1/exchanges", response_model=list[ExchangeView])
def list_exchanges(_: Auth):
    config = _runtime_settings()
    return [
        ExchangeView(
            exchange_id=item.exchange_id,
            adapter=item.adapter,
            symbols=list(item.symbols),
        )
        for item in config.exchanges.values()
    ]


@app.get("/api/v1/instruments", response_model=list[InstrumentView])
def list_instruments(
    _: Auth,
    exchange_id: str = Query(min_length=1, max_length=32),
    symbol: str | None = Query(default=None, min_length=1, max_length=64),
):
    config = _runtime_settings()
    exchange = _exchange_config(config, exchange_id)
    canonical = _canonical_symbol(config, exchange, symbol) if symbol is not None else None
    try:
        return _runtime_pool(config).get(exchange_id).fetch_instruments(canonical)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="symbol is not allowed for exchange") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable") from exc


def _parse_symbols(config, exchange, raw_symbols: str | None) -> list[str]:
    if raw_symbols is None:
        return list(exchange.symbols)
    requested = raw_symbols.split(",")
    if not requested or any(not symbol for symbol in requested):
        raise HTTPException(status_code=422, detail="symbols must be a comma-separated non-empty list")
    resolved = [_canonical_symbol(config, exchange, symbol) for symbol in requested]
    if len(set(resolved)) != len(resolved):
        raise HTTPException(status_code=422, detail="symbols must not contain duplicates")
    return resolved


@app.get("/api/v1/prices", response_model=list[PriceView])
def list_prices(
    _: Auth,
    exchange_id: str = Query(min_length=1, max_length=32),
    symbols: str | None = Query(default=None, min_length=1),
):
    config = _runtime_settings()
    exchange = _exchange_config(config, exchange_id)
    requested = _parse_symbols(config, exchange, symbols)
    try:
        prices = _runtime_pool(config).get(exchange_id).fetch_prices(requested)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="symbol is not allowed for exchange") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="market price is unavailable") from exc
    if len(prices) != len(requested) or any(item.get("mid_price") is None for item in prices):
        raise HTTPException(status_code=503, detail="market price is unavailable")
    return prices


@app.get("/api/v1/balance", response_model=BalanceView)
def balance(db: Db, _: Auth):
    config = _runtime_settings()
    try:
        return calculate_account_balance(db, config, _runtime_pool(config))
    except ValuationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _control_flag(db: Session) -> ControlFlag:
    item = db.get(ControlFlag, 1)
    if item is None:
        item = ControlFlag(id=1)
        db.add(item)
        db.flush()
    return item


def _control_view(item: ControlFlag, *, cancellation_requested_count: int = 0) -> TradingControlView:
    return TradingControlView(
        kill_switch=bool(item.kill_switch),
        close_only=bool(item.close_only),
        reason=item.reason,
        updated_at=item.updated_at,
        cancellation_requested_count=cancellation_requested_count,
    )


def _order_matches_payload(order: Order, payload: OrderCreate, canonical_symbol: str) -> bool:
    return (
        order.exchange_id == payload.exchange_id
        and order.symbol == canonical_symbol
        and order.side == payload.side
        and order.order_type == payload.order_type
        and Decimal(order.quantity) == payload.quantity
        and (Decimal(order.limit_price) if order.limit_price is not None else None) == payload.limit_price
        and order.reduce_only == payload.reduce_only
        and order.strategy_id == payload.strategy_id
    )


def _full_close(db: Session, payload: OrderCreate, canonical_symbol: str) -> bool:
    if not payload.reduce_only:
        return False
    position = db.scalar(
        select(Position).where(
            Position.exchange_id == payload.exchange_id,
            Position.symbol == canonical_symbol,
        )
    )
    if position is None or Decimal(position.quantity) == 0:
        return False
    signed_order = payload.quantity if payload.side == "buy" else -payload.quantity
    return Decimal(position.quantity) * signed_order < 0 and payload.quantity == abs(Decimal(position.quantity))


def _validate_order_instrument(
    db: Session,
    config,
    exchange,
    payload: OrderCreate,
    canonical_symbol: str,
) -> None:
    try:
        adapter = _runtime_pool(config).get(exchange.exchange_id)
        instruments = adapter.fetch_instruments(canonical_symbol)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable") from exc
    if not instruments:
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable")
    instrument = instruments[0]
    price = payload.limit_price
    if payload.order_type == "market" and instrument.get("min_notional") is not None:
        try:
            price_items = adapter.fetch_prices([canonical_symbol])
            price = Decimal(price_items[0]["mid_price"])
        except Exception:
            price = None
            LOGGER.warning(
                "market price unavailable during API notional validation; delegating to executor: exchange=%s symbol=%s",
                exchange.exchange_id,
                canonical_symbol,
                exc_info=True,
            )
    decision = validate_instrument_quantity(
        payload.quantity,
        price,
        instrument,
        is_full_close=_full_close(db, payload, canonical_symbol),
        order_type=payload.order_type,
    )
    if decision.allowed:
        return
    if decision.reason in {"instrument metadata is incomplete", "instrument metadata is invalid"}:
        raise HTTPException(status_code=503, detail=decision.reason)
    raise HTTPException(status_code=422, detail=decision.reason or "instrument quantity is invalid")


@app.post("/api/v1/orders", response_model=OrderView, status_code=status.HTTP_202_ACCEPTED)
def create_order(payload: OrderCreate, response: Response, db: Db, _: Auth):
    config = _runtime_settings()
    existing = db.scalar(select(Order).where(Order.request_id == payload.request_id))
    if existing is not None:
        if payload.exchange_id != existing.exchange_id:
            raise HTTPException(status_code=409, detail="request_id is already used by a different order")
        exchange_config = _exchange_config(config, payload.exchange_id)
        if payload.symbol == existing.symbol:
            canonical_symbol = existing.symbol
        else:
            adapter = _existing_adapter(_runtime_pool(config), payload.exchange_id)
            resolver = getattr(adapter, "resolve_cached_symbol", None) if adapter is not None else None
            canonical_symbol = resolver(payload.symbol) if callable(resolver) else None
            if canonical_symbol is None:
                canonical_symbol = _canonical_symbol(config, exchange_config, payload.symbol)
        if not _order_matches_payload(existing, payload, canonical_symbol):
            raise HTTPException(status_code=409, detail="request_id is already used by a different order")
        response.status_code = status.HTTP_200_OK
        return existing
    exchange_config = _exchange_config(config, payload.exchange_id)
    canonical_symbol = _canonical_symbol(config, exchange_config, payload.symbol)
    control = db.get(ControlFlag, 1)
    if control and control.close_only and not payload.reduce_only:
        raise HTTPException(
            status_code=409,
            detail=f"close-only mode is enabled: {control.reason or 'no reason'}",
        )
    _validate_order_instrument(db, config, exchange_config, payload, canonical_symbol)

    item = Order(
        request_id=payload.request_id,
        strategy_id=payload.strategy_id,
        exchange_id=payload.exchange_id,
        exchange_network=config.exchange_network,
        symbol=canonical_symbol,
        side=payload.side,
        order_type=payload.order_type,
        quantity=payload.quantity,
        limit_price=payload.limit_price,
        reduce_only=payload.reduce_only,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Order).where(Order.request_id == payload.request_id))
        if existing is None:
            raise
        if not _order_matches_payload(existing, payload, canonical_symbol):
            raise HTTPException(status_code=409, detail="request_id is already used by a different order")
        response.status_code = status.HTTP_200_OK
        return existing
    db.refresh(item)
    return item


@app.get("/api/v1/orders/by-request-id/{request_id}", response_model=OrderView)
def get_order_by_request_id(request_id: str, db: Db, _: Auth):
    item = db.scalar(select(Order).where(Order.request_id == request_id))
    if item is None:
        raise HTTPException(status_code=404, detail="order not found")
    return item


@app.get("/api/v1/orders/{order_id}", response_model=OrderView)
def get_order(order_id: str, db: Db, _: Auth):
    item = db.get(Order, order_id)
    if item is None:
        raise HTTPException(status_code=404, detail="order not found")
    return item


@app.post("/api/v1/orders/{order_id}/cancel", response_model=OrderView, status_code=202)
def cancel_order(order_id: str, db: Db, _: Auth):
    item = db.get(Order, order_id)
    if item is None:
        raise HTTPException(status_code=404, detail="order not found")
    if item.status in {"filled", "canceled", "rejected", "failed"}:
        raise HTTPException(status_code=409, detail=f"order is already {item.status}")
    item.cancellation_requested = True
    db.commit()
    db.refresh(item)
    return item


@app.get("/api/v1/orders/{order_id}/fills", response_model=list[FillView])
def list_order_fills(order_id: str, db: Db, _: Auth):
    if db.get(Order, order_id) is None:
        raise HTTPException(status_code=404, detail="order not found")
    return db.scalars(select(Fill).where(Fill.order_id == order_id).order_by(Fill.sequence)).all()


@app.get("/api/v1/history/orders", response_model=OrderHistoryPage)
def list_order_history(
    db: Db,
    _: Auth,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    exchange_id: str | None = Query(default=None, min_length=1, max_length=32),
    symbol: str | None = None,
    side: Literal["buy", "sell"] | None = None,
    order_status: str | None = Query(default=None, alias="status", max_length=32),
    strategy_id: str | None = Query(default=None, min_length=1, max_length=64),
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
):
    start, end = _date_bounds(from_date, to_date)
    query = select(Order)
    if start:
        query = query.where(Order.created_at >= start)
    if end:
        query = query.where(Order.created_at < end)
    if exchange_id:
        query = query.where(Order.exchange_id == exchange_id)
    if symbol:
        query = query.where(_history_symbol_condition(Order, exchange_id, symbol))
    if side:
        query = query.where(Order.side == side)
    if order_status:
        query = query.where(Order.status == order_status)
    if strategy_id:
        query = query.where(Order.strategy_id == strategy_id)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                Order.created_at < cursor_time,
                (Order.created_at == cursor_time) & (Order.id < cursor_id),
            )
        )
    rows = list(db.scalars(query.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit + 1)).all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = _encode_cursor(items[-1].created_at, items[-1].id) if has_more and items else None
    return OrderHistoryPage(items=items, next_cursor=next_cursor)


@app.get("/api/v1/history/fills", response_model=FillHistoryPage)
def list_fill_history(
    db: Db,
    _: Auth,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
    exchange_id: str | None = Query(default=None, min_length=1, max_length=32),
    symbol: str | None = None,
    side: Literal["buy", "sell"] | None = None,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
):
    start, end = _date_bounds(from_date, to_date)
    query = select(Fill)
    if start:
        query = query.where(Fill.executed_at >= start)
    if end:
        query = query.where(Fill.executed_at < end)
    if exchange_id:
        query = query.where(Fill.exchange_id == exchange_id)
    if symbol:
        query = query.where(_history_symbol_condition(Fill, exchange_id, symbol))
    if side:
        query = query.where(Fill.side == side)
    if cursor:
        cursor_time, cursor_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                Fill.executed_at < cursor_time,
                (Fill.executed_at == cursor_time) & (Fill.id < cursor_id),
            )
        )
    rows = list(db.scalars(query.order_by(Fill.executed_at.desc(), Fill.id.desc()).limit(limit + 1)).all())
    has_more = len(rows) > limit
    items = rows[:limit]
    next_cursor = _encode_cursor(items[-1].executed_at, items[-1].id) if has_more and items else None
    return FillHistoryPage(items=items, next_cursor=next_cursor)


@app.get("/api/v1/history/pnl", response_model=PnlHistoryView)
def pnl_history(
    db: Db,
    _: Auth,
    from_date: date | None = Query(default=None, alias="from"),
    to_date: date | None = Query(default=None, alias="to"),
):
    _validate_date_range(from_date, to_date)
    query = select(DailyPnl)
    if from_date:
        query = query.where(DailyPnl.trade_date >= from_date)
    if to_date:
        query = query.where(DailyPnl.trade_date <= to_date)
    rows = list(db.scalars(query.order_by(DailyPnl.trade_date)).all())

    effective_from = from_date or (rows[0].trade_date if rows else None)
    effective_to = to_date or (rows[-1].trade_date if rows else None)
    if effective_from and effective_to:
        _validate_date_range(effective_from, effective_to)

    values = {item.trade_date: Decimal(item.realized_pnl) for item in rows}
    points: list[PnlHistoryPoint] = []
    cumulative = Decimal("0")
    current = effective_from
    while current is not None and effective_to is not None and current <= effective_to:
        daily = values.get(current, Decimal("0"))
        cumulative += daily
        points.append(
            PnlHistoryPoint(
                trade_date=current,
                daily_realized_pnl=daily,
                cumulative_realized_pnl=cumulative,
            )
        )
        current += timedelta(days=1)

    return PnlHistoryView(
        from_date=effective_from,
        to_date=effective_to,
        points=points,
    )


@app.get("/api/v1/fills", response_model=list[FillView])
def list_fills(
    db: Db,
    _: Auth,
    exchange_id: str | None = Query(default=None, min_length=1, max_length=32),
    limit: int = 100,
):
    query = select(Fill)
    if exchange_id:
        query = query.where(Fill.exchange_id == exchange_id)
    query = query.order_by(Fill.executed_at.desc()).limit(min(max(limit, 1), 1000))
    return db.scalars(query).all()


@app.get("/api/v1/positions", response_model=list[PositionView])
def list_positions(
    db: Db,
    _: Auth,
    exchange_id: str | None = Query(default=None, min_length=1, max_length=32),
):
    query = select(Position)
    if exchange_id:
        query = query.where(Position.exchange_id == exchange_id)
    query = query.order_by(Position.exchange_id, Position.symbol)
    return db.scalars(query).all()


def _existing_close_orders(
    db: Session,
    *,
    request_prefix: str,
    exchange_id: str,
    symbol: str | None,
    strategy_id: str | None,
) -> list[Order]:
    items = list(
        db.scalars(
            select(Order)
            .where(Order.request_id.startswith(request_prefix, autoescape=True))
            .order_by(Order.symbol, Order.created_at)
        ).all()
    )
    if not items:
        return []
    if any(
        item.exchange_id != exchange_id
        or item.order_type != "market"
        or not item.reduce_only
        or (symbol is not None and item.symbol != symbol)
        or item.strategy_id != strategy_id
        for item in items
    ):
        raise HTTPException(status_code=409, detail="request_id is already used by a different operation")
    return items


def _close_request_prefix(request_id: str) -> str:
    if len(request_id) <= 64:
        return f"gui-close:{request_id}:"
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    return f"gui-close:{digest}:"


@app.post(
    "/api/v1/positions/close",
    response_model=CloseOrdersView,
    status_code=status.HTTP_202_ACCEPTED,
)
def close_positions(payload: PositionCloseRequest, db: Db, _: Auth):
    config = _runtime_settings()
    exchange_config = _exchange_config(config, payload.exchange_id)
    canonical_symbol = (
        _canonical_symbol(config, exchange_config, payload.symbol)
        if payload.symbol is not None
        else None
    )

    request_prefix = _close_request_prefix(payload.request_id)
    existing = _existing_close_orders(
        db,
        request_prefix=request_prefix,
        exchange_id=payload.exchange_id,
        symbol=canonical_symbol,
        strategy_id=payload.strategy_id,
    )
    if existing:
        return CloseOrdersView(items=existing)

    query = (
        select(Position)
        .where(
            Position.exchange_id == payload.exchange_id,
            Position.quantity != 0,
        )
        .order_by(Position.symbol)
        .with_for_update()
    )
    if canonical_symbol:
        query = query.where(Position.symbol == canonical_symbol)
    positions = list(db.scalars(query).all())
    if not positions:
        raise HTTPException(status_code=409, detail="no open position exists")

    close_payloads = [
        OrderCreate(
            request_id=f"{request_prefix}{position.id}",
            strategy_id=payload.strategy_id,
            exchange_id=position.exchange_id,
            symbol=position.symbol,
            side="sell" if Decimal(position.quantity) > 0 else "buy",
            order_type="market",
            quantity=abs(Decimal(position.quantity)),
            reduce_only=True,
        )
        for position in positions
    ]
    for close_payload in close_payloads:
        _validate_order_instrument(
            db,
            config,
            exchange_config,
            close_payload,
            close_payload.symbol,
        )

    cancel_query = (
        update(Order)
        .where(
            Order.exchange_id == payload.exchange_id,
            Order.status.in_(NON_TERMINAL_ORDER_STATUSES),
        )
        .values(cancellation_requested=True)
    )
    if canonical_symbol:
        cancel_query = cancel_query.where(Order.symbol == canonical_symbol)
    db.execute(cancel_query)

    items = [
        Order(
            request_id=close_payload.request_id,
            strategy_id=close_payload.strategy_id,
            exchange_id=close_payload.exchange_id,
            exchange_network=config.exchange_network,
            symbol=close_payload.symbol,
            side=close_payload.side,
            order_type="market",
            quantity=close_payload.quantity,
            reduce_only=True,
        )
        for close_payload in close_payloads
    ]
    db.add_all(items)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _existing_close_orders(
            db,
            request_prefix=request_prefix,
            exchange_id=payload.exchange_id,
            symbol=canonical_symbol,
            strategy_id=payload.strategy_id,
        )
        if existing:
            return CloseOrdersView(items=existing)
        raise
    for item in items:
        db.refresh(item)
    return CloseOrdersView(items=items)


def _public_exchange(config):
    return _runtime_pool(_runtime_settings()).get(config.exchange_id)


@app.get("/api/v1/current-positions", response_model=CurrentPositionsView)
def current_positions(
    db: Db,
    _: Auth,
    exchange_id: str = Query(min_length=1, max_length=32),
    symbol: str | None = None,
):
    config = _runtime_settings()
    exchange_config = _exchange_config(config, exchange_id)
    canonical_symbol = _canonical_symbol(config, exchange_config, symbol) if symbol else None

    query = select(Position).where(
        Position.exchange_id == exchange_id,
        Position.quantity != 0,
    )
    if canonical_symbol:
        query = query.where(Position.symbol == canonical_symbol)
    stored_positions = list(db.scalars(query.order_by(Position.symbol)).all())
    refreshed_at = datetime.now(timezone.utc)
    if not stored_positions:
        return CurrentPositionsView(
            exchange_id=exchange_id,
            refreshed_at=refreshed_at,
            valuation_complete=True,
            unpriced_count=0,
            total_unrealized_pnl=Decimal("0"),
            positions=[],
        )

    adapter = None
    adapter_error = False
    try:
        adapter = _public_exchange(exchange_config)
    except Exception:
        adapter_error = True
        LOGGER.exception("public market adapter initialization failed")

    items: list[CurrentPositionView] = []
    for stored in stored_positions:
        quantity = Decimal(stored.quantity)
        average_entry_price = Decimal(stored.average_entry_price)
        base_asset, quote_asset = market_assets(None, stored.symbol)
        if adapter is not None:
            try:
                base_asset, quote_asset = market_assets(adapter.market(stored.symbol), stored.symbol)
            except Exception:
                LOGGER.warning("market metadata lookup failed: symbol=%s", stored.symbol, exc_info=True)

        current_price = None
        position_pnl = None
        price_observed_at = None
        valuation_status = "unavailable"
        valuation_error = "market data is unavailable" if adapter_error else None
        if adapter is not None:
            try:
                book = adapter.fetch_order_book(stored.symbol)
                current_price, price_observed_at = order_book_midpoint(
                    book,
                    now=datetime.now(timezone.utc),
                    max_age_seconds=config.market_data_max_age_seconds,
                )
                position_pnl = unrealized_pnl(quantity, average_entry_price, current_price)
                valuation_status = "ok"
            except ValuationError as exc:
                valuation_error = str(exc)
                LOGGER.warning("position valuation rejected: symbol=%s reason=%s", stored.symbol, exc)
            except Exception:
                valuation_error = "market data is unavailable"
                LOGGER.warning("position valuation failed: symbol=%s", stored.symbol, exc_info=True)

        items.append(
            CurrentPositionView(
                exchange_id=stored.exchange_id,
                symbol=stored.symbol,
                base_asset=base_asset,
                quote_asset=quote_asset,
                position_side=position_side(quantity),
                quantity=quantity,
                average_entry_price=average_entry_price,
                current_price=current_price,
                unrealized_pnl=position_pnl,
                position_updated_at=stored.updated_at,
                price_observed_at=price_observed_at,
                valuation_status=valuation_status,
                valuation_error=valuation_error,
            )
        )

    unpriced_count = sum(item.valuation_status != "ok" for item in items)
    total = None
    if unpriced_count == 0:
        total = sum((item.unrealized_pnl or Decimal("0") for item in items), Decimal("0"))
    return CurrentPositionsView(
        exchange_id=exchange_id,
        refreshed_at=datetime.now(timezone.utc),
        valuation_complete=unpriced_count == 0,
        unpriced_count=unpriced_count,
        total_unrealized_pnl=total,
        positions=items,
    )


@app.get("/api/v1/pnl")
def pnl(db: Db, _: Auth):
    positions = db.scalars(select(Position)).all()
    return {
        "realized_pnl": str(sum((Decimal(item.realized_pnl) for item in positions), Decimal("0"))),
        "unrealized_pnl": None,
        "note": "unrealized PnL requires a current market price and is not included in this endpoint",
    }


@app.get("/api/v1/trading-control", response_model=TradingControlView)
def trading_control(db: Db, _: Auth):
    return _control_view(_control_flag(db))


@app.post("/api/v1/close-only", response_model=TradingControlView)
def set_close_only(payload: KillSwitchRequest, db: Db, _: Auth):
    item = _control_flag(db)
    item.close_only = payload.enabled
    if payload.enabled:
        item.kill_switch = False
    item.reason = payload.reason
    cancellation_requested_count = 0
    if payload.enabled:
        result = db.execute(
            update(Order)
            .where(
                Order.status.in_(NON_TERMINAL_ORDER_STATUSES),
                Order.reduce_only.is_(False),
                Order.cancellation_requested.is_(False),
            )
            .values(cancellation_requested=True)
        )
        cancellation_requested_count = int(result.rowcount or 0)
    db.commit()
    db.refresh(item)
    return _control_view(item, cancellation_requested_count=cancellation_requested_count)


@app.post("/api/v1/kill-switch", response_model=TradingControlView)
def set_kill_switch(payload: KillSwitchRequest, db: Db, _: Auth):
    item = _control_flag(db)
    item.kill_switch = payload.enabled
    if payload.enabled:
        item.close_only = False
    item.reason = payload.reason
    db.commit()
    db.refresh(item)
    return _control_view(item)
