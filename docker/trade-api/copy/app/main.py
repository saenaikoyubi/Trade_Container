from __future__ import annotations

import base64
import binascii
import asyncio
import json
import hmac
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from trade_common.config import read_secret, settings
from trade_common.close_service import active_parent_for_position, all_children, append_initial_children, cancel_parent, refresh_parent
from trade_common.database import engine, session_factory
from trade_common.exchange_adapters import ExchangeAdapterPool
from trade_common.logging_config import configure_logging
from trade_common.market_rules import (
    ACTIVE_CLOSE_STATUSES, MarketRuleError, TERMINAL_ORDER_STATUSES,
    bybit_history_symbols, bybit_local_canonical, fresh_mark_price, positive_decimal,
)
from trade_common.models import CloseRequest, CloseRequestPosition, ControlFlag, DailyPnl, Fill, Order, Position, RequestKey
from trade_common.risk import instrument_preflight_reason, validate_instrument_market, validate_instrument_quantity, validate_limit_price
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


async def _warm_bybit_once(application: FastAPI) -> bool:
    pool = getattr(application.state, "adapter_pool", None)
    if pool is None:
        return True
    try:
        adapter = await asyncio.to_thread(pool.get, "bybit")
        refresh = getattr(adapter, "refresh_metadata", None)
        await asyncio.to_thread(refresh if callable(refresh) else adapter.fetch_instruments)
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
            application.state.runtime_config.exchanges["bybit"].metadata_ttl_seconds
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


@app.exception_handler(MarketRuleError)
def market_rule_error(_request, exc: MarketRuleError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "reason_code": exc.reason_code})


@app.exception_handler(SQLAlchemyError)
def database_error(_request, _exc: SQLAlchemyError):
    LOGGER.exception("database operation failed")
    return JSONResponse(status_code=503, content={"detail": "database is unavailable"})


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
    base_asset: str | None
    quote_asset: str | None
    position_side: Literal["buy", "sell"]
    quantity: Decimal
    average_entry_price: Decimal
    current_price: Decimal | None
    unrealized_pnl: Decimal | None
    position_updated_at: datetime
    price_observed_at: datetime | None
    valuation_status: Literal["ok", "unavailable"]
    valuation_reason_code: str | None
    valuation_detail: str | None


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
    metadata_age_seconds: float | None = None
    metadata_stale: bool = False
    new_or_increase_allowed: bool
    new_or_increase_reason_code: str | None
    market_new_or_increase_allowed: bool
    market_new_or_increase_reason_code: str | None
    limit_new_or_increase_allowed: bool
    limit_new_or_increase_reason_code: str | None


class PriceView(BaseModel):
    exchange_id: str
    symbol: str
    mark_price: Decimal | None
    mark_observed_at: datetime | None
    last_price: Decimal | None
    bid_price: Decimal | None
    ask_price: Decimal | None
    mid_price: Decimal | None
    observed_at: datetime | None


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


class PnlView(BaseModel):
    currency: str
    realized_pnl: Decimal


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


class TradingControlUpdateView(TradingControlView):
    cancellation_requested_count: int


class FillPage(BaseModel):
    items: list[FillView]
    next_cursor: str | None


class ClosePositionView(BaseModel):
    symbol: str
    initial_position_quantity: Decimal
    remaining_position_quantity: Decimal
    status: str
    reason_code: str | None
    detail: str | None


class CloseRequestView(BaseModel):
    request_id: str
    exchange_id: str
    symbol: str | None
    strategy_id: str | None
    status: str
    generation_mode: Literal["eager", "incremental"]
    positions: list[ClosePositionView]
    items: list[OrderView]
    reason_code: str | None
    detail: str | None
    created_at: datetime
    updated_at: datetime


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
    if runtime_config is not None:
        return runtime_config
    try:
        return settings()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="configuration is unavailable") from exc


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
    if exchange.exchange_id != "bybit" and raw_symbol in exchange.symbols:
        return raw_symbol
    try:
        return _runtime_pool(config).get(exchange.exchange_id).resolve_symbol(raw_symbol)
    except MarketRuleError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="symbol is not allowed for exchange") from exc
    except RuntimeError as exc:
        if "unsupported CCXT exchange" in str(exc):
            raise HTTPException(status_code=422, detail="symbol is not allowed for exchange") from exc
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable") from exc


def _history_symbol_condition(model, exchange_id: str | None, raw_symbol: str):
    if not raw_symbol or raw_symbol != raw_symbol.strip():
        raise HTTPException(status_code=422, detail="symbol must not contain surrounding whitespace")
    aliases = bybit_history_symbols(raw_symbol)
    if exchange_id == "bybit":
        return model.symbol.in_(aliases)
    if exchange_id is not None or len(aliases) == 1:
        return model.symbol == raw_symbol
    return or_(model.symbol == raw_symbol, and_(model.exchange_id == "bybit", model.symbol == aliases[1]))


def _validate_date_range(from_date: date | None, to_date: date | None) -> None:
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=422, detail="from must not be after to")
    if from_date and to_date and (to_date - from_date).days + 1 > 3660:
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


def _close_view(db: Session, parent: CloseRequest) -> CloseRequestView:
    targets = list(db.scalars(select(CloseRequestPosition).where(
        CloseRequestPosition.close_request_id == parent.id
    ).order_by(CloseRequestPosition.symbol)).all())
    return CloseRequestView(
        request_id=parent.request_id, exchange_id=parent.exchange_id,
        symbol=parent.symbol, strategy_id=parent.strategy_id,
        status=parent.status, generation_mode=parent.generation_mode,
        positions=[ClosePositionView.model_validate({
            "symbol": item.symbol,
            "initial_position_quantity": item.initial_position_quantity,
            "remaining_position_quantity": item.remaining_position_quantity,
            "status": item.status, "reason_code": item.reason_code, "detail": item.detail,
        }) for item in targets],
        items=[OrderView.model_validate(item) for item in all_children(db, parent.id)],
        reason_code=parent.reason_code, detail=parent.detail,
        created_at=parent.created_at, updated_at=parent.updated_at,
    )


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
        items = _runtime_pool(config).get(exchange_id).fetch_instruments(canonical)
        return [_instrument_view(item) for item in items]
    except MarketRuleError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="symbol is not allowed for exchange") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable") from exc


def _instrument_view(item: dict) -> dict:
    result = dict(item)
    market_reason = instrument_preflight_reason(item, "market")
    limit_reason = instrument_preflight_reason(item, "limit")
    result["metadata_stale"] = bool(item.get("metadata_stale"))
    result["market_new_or_increase_allowed"] = market_reason is None
    result["market_new_or_increase_reason_code"] = market_reason
    result["limit_new_or_increase_allowed"] = limit_reason is None
    result["limit_new_or_increase_reason_code"] = limit_reason
    result["new_or_increase_allowed"] = market_reason is None and limit_reason is None
    result["new_or_increase_reason_code"] = market_reason or limit_reason
    return result


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
    except MarketRuleError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="symbol is not allowed for exchange") from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="market price is unavailable") from exc
    if len(prices) != len(requested):
        raise HTTPException(status_code=503, detail="market price is unavailable")
    if exchange_id == "bybit":
        if any(positive_decimal(item.get("mark_price")) is None or item.get("mark_observed_at") is None for item in prices):
            raise MarketRuleError("mark_price_unavailable", "fresh Mark Price is unavailable", 503)
    elif any(item.get("mid_price") is None or item.get("observed_at") is None for item in prices):
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


def _control_view(item: ControlFlag) -> TradingControlView:
    return TradingControlView(
        kill_switch=bool(item.kill_switch),
        close_only=bool(item.close_only),
        reason=item.reason,
        updated_at=item.updated_at,
    )


def _control_update_view(item: ControlFlag, count: int) -> TradingControlUpdateView:
    return TradingControlUpdateView(**_control_view(item).model_dump(), cancellation_requested_count=count)


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
) -> dict:
    try:
        adapter = _runtime_pool(config).get(exchange.exchange_id)
        instruments = adapter.fetch_instruments(canonical_symbol)
    except MarketRuleError:
        raise
    except Exception as exc:
        if exchange.exchange_id == "bybit":
            raise MarketRuleError("instrument_data_unavailable", f"instrument metadata is unavailable for {canonical_symbol}", 503) from exc
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable") from exc
    if not instruments:
        if exchange.exchange_id == "bybit":
            raise MarketRuleError("instrument_data_unavailable", f"instrument metadata is unavailable for {canonical_symbol}", 503)
        raise HTTPException(status_code=503, detail="instrument metadata is unavailable")
    instrument = instruments[0]
    position = db.scalar(select(Position).where(
        Position.exchange_id == payload.exchange_id, Position.symbol == canonical_symbol
    ))
    position_quantity = Decimal(position.quantity) if position is not None else Decimal("0")
    market_decision = validate_instrument_market(
        instrument, exchange_id=payload.exchange_id, order_type=payload.order_type,
        reduce_only=payload.reduce_only, position_quantity=position_quantity,
        side=payload.side, quantity=payload.quantity,
    )
    if not market_decision.allowed:
        if exchange.exchange_id == "bybit" and market_decision.reason_code:
            raise MarketRuleError(market_decision.reason_code, market_decision.reason or "instrument is not tradable", 503 if market_decision.temporary else 422)
        raise HTTPException(status_code=503 if market_decision.temporary else 422, detail=market_decision.reason)
    if payload.order_type == "limit":
        price_decision = validate_limit_price(payload.limit_price, instrument, exchange_id=payload.exchange_id)
        if not price_decision.allowed:
            if exchange.exchange_id == "bybit" and price_decision.reason_code:
                raise MarketRuleError(price_decision.reason_code, price_decision.reason or "limit price is invalid", 503 if price_decision.temporary else 422)
            raise HTTPException(status_code=503 if price_decision.temporary else 422, detail=price_decision.reason)
    is_full_close = _full_close(db, payload, canonical_symbol)
    price = payload.limit_price
    if payload.order_type == "market" and exchange.exchange_id == "bybit" and instrument.get("status") != "active":
        price, _ = fresh_mark_price(adapter, canonical_symbol)
    elif payload.order_type == "market" and instrument.get("min_notional") is not None and not is_full_close:
        try:
            book = adapter.fetch_order_book(canonical_symbol)
            price, _ = order_book_midpoint(
                book, now=datetime.now(timezone.utc),
                max_age_seconds=config.market_data_max_age_seconds,
            )
        except Exception as exc:
            if exchange.exchange_id == "bybit":
                raise MarketRuleError("order_book_unavailable", f"fresh order book is unavailable for {canonical_symbol}", 503) from exc
            raise HTTPException(status_code=503, detail="market price is unavailable") from exc
    decision = validate_instrument_quantity(
        payload.quantity,
        price,
        instrument,
        is_full_close=is_full_close,
        order_type=payload.order_type,
    )
    if decision.allowed:
        return instrument
    if exchange.exchange_id == "bybit" and decision.reason_code:
        raise MarketRuleError(decision.reason_code, decision.reason or "instrument metadata is invalid", 503 if decision.temporary else 422)
    raise HTTPException(status_code=503 if decision.temporary else 422, detail=decision.reason or "instrument quantity is invalid")


@app.post("/api/v1/orders", response_model=OrderView, status_code=status.HTTP_202_ACCEPTED)
def create_order(payload: OrderCreate, response: Response, db: Db, _: Auth):
    key = db.get(RequestKey, payload.request_id)
    if key is not None and key.operation_kind == "close_request":
        raise HTTPException(status_code=409, detail="request_id is already used by a close request")
    if db.scalar(select(CloseRequest).where(CloseRequest.request_id == payload.request_id)) is not None:
        raise HTTPException(status_code=409, detail="request_id is already used by a close request")
    existing = db.scalar(select(Order).where(Order.request_id == payload.request_id))
    if existing is not None:
        if payload.exchange_id != existing.exchange_id:
            raise HTTPException(status_code=409, detail="request_id is already used by a different order")
        if payload.exchange_id == "bybit":
            canonical_symbol = bybit_local_canonical(payload.symbol) or payload.symbol
        elif payload.symbol == existing.symbol:
            canonical_symbol = existing.symbol
        else:
            config = _runtime_settings()
            exchange_config = _exchange_config(config, payload.exchange_id)
            adapter = _existing_adapter(_runtime_pool(config), payload.exchange_id)
            resolver = getattr(adapter, "resolve_cached_symbol", None) if adapter is not None else None
            canonical_symbol = resolver(payload.symbol) if callable(resolver) else None
            if canonical_symbol is None:
                canonical_symbol = _canonical_symbol(config, exchange_config, payload.symbol)
        if not _order_matches_payload(existing, payload, canonical_symbol):
            raise HTTPException(status_code=409, detail="request_id is already used by a different order")
        response.status_code = status.HTTP_200_OK
        return existing
    if key is not None:
        raise HTTPException(status_code=409, detail="request_id is already used")
    config = _runtime_settings()
    exchange_config = _exchange_config(config, payload.exchange_id)
    canonical_symbol = _canonical_symbol(config, exchange_config, payload.symbol)
    preflight_position = db.scalar(select(Position).where(
        Position.exchange_id == payload.exchange_id, Position.symbol == canonical_symbol
    ))
    preflight_quantity = Decimal(preflight_position.quantity) if preflight_position is not None else Decimal("0")
    _validate_order_instrument(db, config, exchange_config, payload, canonical_symbol)
    db.rollback()
    control = db.get(ControlFlag, 1, with_for_update=True)
    if control and control.kill_switch:
        raise HTTPException(status_code=409, detail="kill switch is enabled")
    if control and control.close_only and not payload.reduce_only:
        raise HTTPException(
            status_code=409,
            detail=f"close-only mode is enabled: {control.reason or 'no reason'}",
        )
    position = db.scalar(select(Position).where(
        Position.exchange_id == payload.exchange_id, Position.symbol == canonical_symbol
    ).with_for_update())
    current_quantity = Decimal(position.quantity) if position is not None else Decimal("0")
    if current_quantity != preflight_quantity:
        raise HTTPException(status_code=409, detail="position changed during order validation; retry with the same request_id")
    if position is not None and active_parent_for_position(db, position.id) is not None:
        raise HTTPException(status_code=409, detail="a close request is already active for this position")

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
        db.flush()
        db.add(RequestKey(request_id=payload.request_id, operation_kind="order", target_id=item.id))
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(Order).where(Order.request_id == payload.request_id))
        if existing is None:
            raise HTTPException(status_code=409, detail="request_id or close request conflicts with an existing operation")
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
def cancel_order(order_id: str, response: Response, db: Db, _: Auth):
    snapshot = db.get(Order, order_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="order not found")
    exchange_id, symbol = snapshot.exchange_id, snapshot.symbol
    parent_id = snapshot.close_request_id
    db.expire_all()
    db.get(ControlFlag, 1, with_for_update=True)
    db.scalar(select(Position).where(
        Position.exchange_id == exchange_id, Position.symbol == symbol
    ).with_for_update())
    parent = db.get(CloseRequest, parent_id, with_for_update=True) if parent_id else None
    item = db.get(Order, order_id, with_for_update=True)
    if item.status == "canceled":
        response.status_code = 200
        return item
    if item.status in {"filled", "rejected", "failed"}:
        raise HTTPException(status_code=409, detail=f"order is already {item.status}")
    item.cancellation_requested = True
    if parent is not None:
        cancel_parent(db, parent, "a child order was canceled")
    db.commit()
    db.refresh(item)
    return item


@app.get("/api/v1/orders/{order_id}/fills", response_model=FillPage)
def list_order_fills(
    order_id: str, db: Db, _: Auth,
    cursor: str | None = None,
    limit: int = Query(default=100, ge=1, le=200),
):
    if db.get(Order, order_id) is None:
        raise HTTPException(status_code=404, detail="order not found")
    after_sequence = 0
    if cursor is not None:
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            payload = json.loads(raw.decode("utf-8"))
            if payload["order_id"] != order_id or not isinstance(payload["sequence"], int) or payload["sequence"] < 1:
                raise ValueError("cursor belongs to another order")
            after_sequence = payload["sequence"]
        except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid cursor") from exc
    rows = list(db.scalars(select(Fill).where(
        Fill.order_id == order_id, Fill.sequence > after_sequence
    ).order_by(Fill.sequence).limit(limit + 1)).all())
    items = rows[:limit]
    next_cursor = None
    if len(rows) > limit and items:
        payload = json.dumps({"order_id": order_id, "sequence": items[-1].sequence}, separators=(",", ":"))
        next_cursor = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return FillPage(items=items, next_cursor=next_cursor)


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
    if symbol is not None:
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
    if symbol is not None:
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
    today = datetime.now(timezone.utc).date()
    effective_to = to_date or today
    effective_from = from_date or effective_to - timedelta(days=364)
    if effective_to > today or effective_from > today:
        raise HTTPException(status_code=422, detail="future dates are not allowed")
    _validate_date_range(effective_from, effective_to)
    query = select(DailyPnl).where(
        DailyPnl.trade_date >= effective_from,
        DailyPnl.trade_date <= effective_to,
    )
    rows = list(db.scalars(query.order_by(DailyPnl.trade_date)).all())
    values = {item.trade_date: Decimal(item.realized_pnl) for item in rows}
    points: list[PnlHistoryPoint] = []
    cumulative = Decimal(db.scalar(select(func.coalesce(func.sum(DailyPnl.realized_pnl), 0)).where(
        DailyPnl.trade_date < effective_from
    )) or 0)
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
    symbol: str | None = None,
):
    query = select(Position)
    if exchange_id:
        query = query.where(Position.exchange_id == exchange_id)
    if symbol is not None:
        query = query.where(_history_symbol_condition(Position, exchange_id, symbol))
    query = query.order_by(Position.exchange_id, Position.symbol)
    return db.scalars(query).all()


def _same_close_input(parent: CloseRequest, payload: PositionCloseRequest) -> bool:
    if parent.exchange_id != payload.exchange_id or parent.strategy_id != payload.strategy_id:
        return False
    if payload.symbol is None:
        return parent.symbol is None
    if parent.symbol is None:
        return False
    if payload.symbol in {parent.submitted_symbol, parent.symbol}:
        return True
    return payload.exchange_id == "bybit" and bybit_local_canonical(payload.symbol) == parent.symbol


def _close_market_cap(exchange_id: str, symbol: str, quantity: Decimal, instrument: dict) -> Decimal:
    decision = validate_instrument_quantity(
        min(quantity, positive_decimal(instrument.get("max_market_qty")) or quantity),
        None, instrument, is_close_child=True, order_type="market",
    )
    if decision.temporary:
        if exchange_id == "bybit":
            raise MarketRuleError(decision.reason_code or "instrument_metadata_invalid", decision.reason or "instrument metadata is invalid", 503)
        raise HTTPException(status_code=503, detail=decision.reason)
    cap = quantity
    if exchange_id == "bybit":
        market_cap = positive_decimal(instrument.get("max_market_qty"))
        if market_cap is None:
            raise MarketRuleError("instrument_metadata_invalid", f"max_market_qty is missing or invalid for {symbol}", 503)
        cap = min(cap, market_cap)
    max_qty = instrument.get("max_qty")
    if max_qty is not None:
        parsed = positive_decimal(max_qty)
        if parsed is None:
            if exchange_id == "bybit":
                raise MarketRuleError("instrument_metadata_invalid", f"max_qty is invalid for {symbol}", 503)
            raise HTTPException(status_code=503, detail="max_qty is invalid")
        cap = min(cap, parsed)
    return cap


@app.post(
    "/api/v1/positions/close",
    response_model=CloseRequestView,
    status_code=status.HTTP_202_ACCEPTED,
)
def close_positions(payload: PositionCloseRequest, response: Response, db: Db, _: Auth):
    return _create_close_positions(payload, response, db, generation_mode="eager")


@app.post(
    "/api/v2/positions/close",
    response_model=CloseRequestView,
    status_code=status.HTTP_202_ACCEPTED,
)
def close_positions_v2(payload: PositionCloseRequest, response: Response, db: Db, _: Auth):
    return _create_close_positions(payload, response, db, generation_mode="incremental")


def _create_close_positions(payload: PositionCloseRequest, response: Response, db: Session, *, generation_mode: str):
    key = db.get(RequestKey, payload.request_id)
    existing_parent = db.scalar(select(CloseRequest).where(CloseRequest.request_id == payload.request_id))
    if existing_parent is not None:
        if existing_parent.generation_mode != generation_mode or not _same_close_input(existing_parent, payload):
            raise HTTPException(status_code=409, detail="request_id is already used by a different close request")
        response.status_code = 200
        return _close_view(db, existing_parent)
    if key is not None or db.scalar(select(Order).where(Order.request_id == payload.request_id)) is not None:
        raise HTTPException(status_code=409, detail="request_id is already used by an order")
    config = _runtime_settings()
    exchange_config = _exchange_config(config, payload.exchange_id)
    canonical_symbol = (
        _canonical_symbol(config, exchange_config, payload.symbol)
        if payload.symbol is not None
        else None
    )

    query = (
        select(Position)
        .where(
            Position.exchange_id == payload.exchange_id,
            Position.quantity != 0,
        )
        .order_by(Position.symbol)
    )
    if canonical_symbol:
        query = query.where(Position.symbol == canonical_symbol)
    positions = list(db.scalars(query).all())
    position_snapshot = [(item.id, Decimal(item.quantity), item.symbol) for item in positions]

    caps: dict[str, Decimal] = {}
    if positions:
        adapter = _runtime_pool(config).get(payload.exchange_id)
        for position in positions:
            symbol = position.symbol
            quantity = abs(Decimal(position.quantity))
            try:
                instrument = adapter.fetch_instruments(symbol)[0]
            except MarketRuleError:
                raise
            except Exception as exc:
                if payload.exchange_id == "bybit":
                    raise MarketRuleError("instrument_data_unavailable", f"instrument metadata is unavailable for {symbol}", 503) from exc
                raise HTTPException(status_code=503, detail="instrument metadata is unavailable") from exc
            market_decision = validate_instrument_market(
                instrument, exchange_id=payload.exchange_id, order_type="market", reduce_only=True,
                position_quantity=Decimal(position.quantity),
                side="sell" if position.quantity > 0 else "buy", quantity=quantity,
            )
            if not market_decision.allowed:
                if payload.exchange_id == "bybit" and market_decision.reason_code:
                    raise MarketRuleError(market_decision.reason_code, market_decision.reason or "unsupported market", 503 if market_decision.temporary else 422)
                raise HTTPException(status_code=503 if market_decision.temporary else 422, detail=market_decision.reason)
            caps[symbol] = _close_market_cap(payload.exchange_id, symbol, quantity, instrument)
            if payload.exchange_id == "bybit" and instrument.get("status") != "active":
                fresh_mark_price(adapter, symbol)
            else:
                try:
                    book = adapter.fetch_order_book(symbol)
                    order_book_midpoint(
                        book, now=datetime.now(timezone.utc),
                        max_age_seconds=config.market_data_max_age_seconds,
                    )
                except Exception as exc:
                    if payload.exchange_id == "bybit":
                        raise MarketRuleError("order_book_unavailable", f"fresh order book is unavailable for {symbol}", 503) from exc
                    raise HTTPException(status_code=503, detail="market price is unavailable") from exc

    db.rollback()
    # Incremental requests hold the control lock only while registering one
    # child per position. Eager v1 requests acquire it immediately before the
    # final cancellation update and commit, after the large child batch.
    if generation_mode == "incremental":
        control = db.get(ControlFlag, 1, with_for_update=True)
        if control is not None and control.kill_switch:
            raise HTTPException(status_code=409, detail="kill switch is enabled")
    positions = list(db.scalars(query.with_for_update()).all())
    if [(item.id, Decimal(item.quantity), item.symbol) for item in positions] != position_snapshot:
        raise HTTPException(status_code=409, detail="positions changed during close validation; retry with the same request_id")
    for position in positions:
        if active_parent_for_position(db, position.id) is not None:
            raise HTTPException(status_code=409, detail="a close request is already active for this position")

    parent = CloseRequest(
        id=str(uuid.uuid4()),
        request_id=payload.request_id, exchange_id=payload.exchange_id,
        symbol=canonical_symbol, submitted_symbol=payload.symbol,
        strategy_id=payload.strategy_id, status="queued" if positions else "completed",
        generation_mode=generation_mode,
    )
    db.add(parent)
    try:
        db.flush()
        db.add(RequestKey(request_id=payload.request_id, operation_kind="close_request", target_id=parent.id))
        for position in positions:
            target = CloseRequestPosition(
                id=str(uuid.uuid4()),
                close_request_id=parent.id, position_id=position.id, symbol=position.symbol,
                initial_position_quantity=position.quantity,
                remaining_position_quantity=position.quantity, status="queued",
            )
            db.add(target)
            db.flush()
            append_initial_children(
                db, parent, target,
                quantity=abs(Decimal(position.quantity)), cap=caps[position.symbol],
                network=config.exchange_network,
                incremental=generation_mode == "incremental",
            )
        db.flush()
        if generation_mode == "eager":
            try:
                control = db.scalar(select(ControlFlag).where(ControlFlag.id == 1).with_for_update(nowait=True))
            except OperationalError as exc:
                if getattr(exc.orig, "sqlstate", None) == "55P03":
                    db.rollback()
                    raise HTTPException(status_code=409, detail="trading control is busy; retry with the same request_id") from exc
                raise
            if control is not None and control.kill_switch:
                raise HTTPException(status_code=409, detail="kill switch is enabled")
        for position in positions:
            db.execute(update(Order).where(
                Order.exchange_id == payload.exchange_id,
                Order.symbol == position.symbol,
                Order.close_request_id.is_(None),
                Order.status.in_(NON_TERMINAL_ORDER_STATUSES),
            ).values(cancellation_requested=True))
        db.commit()
    except IntegrityError:
        db.rollback()
        existing_parent = db.scalar(select(CloseRequest).where(CloseRequest.request_id == payload.request_id))
        if existing_parent is not None and existing_parent.generation_mode == generation_mode and _same_close_input(existing_parent, payload):
            response.status_code = 200
            return _close_view(db, existing_parent)
        raise HTTPException(status_code=409, detail="close request conflicts with an existing operation")
    db.refresh(parent)
    if not positions:
        response.status_code = 200
    return _close_view(db, parent)


@app.get("/api/v2/close-requests/{request_id}", response_model=CloseRequestView)
@app.get("/api/v1/close-requests/{request_id}", response_model=CloseRequestView)
def get_close_request(request_id: str, db: Db, _: Auth):
    parent = db.scalar(select(CloseRequest).where(CloseRequest.request_id == request_id))
    if parent is None:
        raise HTTPException(status_code=404, detail="close request not found")
    return _close_view(db, parent)


@app.post("/api/v2/close-requests/{request_id}/cancel", response_model=CloseRequestView, status_code=202)
@app.post("/api/v1/close-requests/{request_id}/cancel", response_model=CloseRequestView, status_code=202)
def cancel_close_request(request_id: str, response: Response, db: Db, _: Auth):
    db.get(ControlFlag, 1, with_for_update=True)
    parent = db.scalar(select(CloseRequest).where(CloseRequest.request_id == request_id).with_for_update())
    if parent is None:
        raise HTTPException(status_code=404, detail="close request not found")
    if parent.status == "canceled":
        response.status_code = 200
        return _close_view(db, parent)
    if parent.status in {"completed", "failed"}:
        raise HTTPException(status_code=409, detail=f"close request is already {parent.status}")
    cancel_parent(db, parent, "close request was canceled")
    db.commit()
    db.refresh(parent)
    return _close_view(db, parent)


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
    exchange_config = config.exchange(exchange_id)
    has_saved_row = db.scalar(select(Position.id).where(Position.exchange_id == exchange_id).limit(1)) is not None
    if exchange_config is None and not has_saved_row:
        raise HTTPException(status_code=422, detail="exchange is not configured and has no saved positions")
    query = select(Position).where(
        Position.exchange_id == exchange_id,
        Position.quantity != 0,
    )
    if symbol is not None:
        query = query.where(_history_symbol_condition(Position, exchange_id, symbol))
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
    if exchange_config is not None:
        try:
            adapter = _runtime_pool(config).get(exchange_id)
        except Exception:
            LOGGER.warning("public market adapter initialization failed: exchange=%s", exchange_id, exc_info=True)

    items: list[CurrentPositionView] = []
    for stored in stored_positions:
        quantity = Decimal(stored.quantity)
        average_entry_price = Decimal(stored.average_entry_price)
        base_asset = quote_asset = None
        current_price = None
        position_pnl = None
        price_observed_at = None
        valuation_status = "unavailable"
        reason_code = "exchange_not_configured" if exchange_config is None else "instrument_data_unavailable"
        detail = "exchange is not configured" if exchange_config is None else "instrument metadata is unavailable"
        if adapter is not None:
            try:
                instrument = adapter.fetch_instruments(stored.symbol)[0]
                base_asset = instrument.get("base_asset") or None
                quote_asset = instrument.get("quote_asset") or None
                settle = instrument.get("settle_asset") or (quote_asset if exchange_id != "bybit" else None)
                if not base_asset or not quote_asset or not settle:
                    reason_code = "instrument_metadata_invalid"
                    raise ValuationError("valuation instrument metadata is incomplete")
                if settle not in {"USD", "USDC", "USDT"}:
                    reason_code = "unsupported_settlement_currency"
                    raise ValuationError(f"unsupported settlement currency: {settle}")
                if exchange_id == "bybit":
                    reason_code = "mark_price_unavailable"
                    current_price, price_observed_at = fresh_mark_price(adapter, stored.symbol)
                else:
                    reason_code = "order_book_unavailable"
                    book = adapter.fetch_order_book(stored.symbol)
                    current_price, price_observed_at = order_book_midpoint(
                        book, now=datetime.now(timezone.utc),
                        max_age_seconds=config.market_data_max_age_seconds,
                    )
                position_pnl = unrealized_pnl(quantity, average_entry_price, current_price)
                valuation_status = "ok"
                reason_code = detail = None
            except Exception as exc:
                detail = str(exc) or "market data is unavailable"
                LOGGER.warning("position valuation failed: symbol=%s reason=%s", stored.symbol, detail)

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
                valuation_reason_code=reason_code,
                valuation_detail=detail,
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


@app.get("/api/v1/pnl", response_model=PnlView)
def pnl(db: Db, _: Auth):
    config = _runtime_settings()
    realized = db.scalar(select(func.coalesce(func.sum(DailyPnl.realized_pnl), 0))) or 0
    return PnlView(currency=config.account.currency, realized_pnl=Decimal(realized))


@app.get("/api/v1/trading-control", response_model=TradingControlView)
def trading_control(db: Db, _: Auth):
    return _control_view(_control_flag(db))


@app.post("/api/v1/close-only", response_model=TradingControlUpdateView)
def set_close_only(payload: KillSwitchRequest, db: Db, _: Auth):
    item = db.get(ControlFlag, 1, with_for_update=True) or _control_flag(db)
    if item.close_only != payload.enabled:
        item.close_only = payload.enabled
    if payload.enabled:
        if item.kill_switch:
            item.kill_switch = False
        if item.reason != payload.reason:
            item.reason = payload.reason
    elif not item.kill_switch and item.reason is not None:
        item.reason = None
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
    return _control_update_view(item, cancellation_requested_count)


@app.post("/api/v1/kill-switch", response_model=TradingControlUpdateView)
def set_kill_switch(payload: KillSwitchRequest, db: Db, _: Auth):
    item = db.get(ControlFlag, 1, with_for_update=True) or _control_flag(db)
    if item.kill_switch != payload.enabled:
        item.kill_switch = payload.enabled
    if payload.enabled:
        if item.close_only:
            item.close_only = False
        if item.reason != payload.reason:
            item.reason = payload.reason
    elif not item.close_only and item.reason is not None:
        item.reason = None
    cancellation_requested_count = 0
    if payload.enabled:
        result = db.execute(update(Order).where(
            Order.status.in_(NON_TERMINAL_ORDER_STATUSES),
            Order.cancellation_requested.is_(False),
        ).values(cancellation_requested=True))
        cancellation_requested_count = int(result.rowcount or 0)
        db.execute(update(CloseRequest).where(
            CloseRequest.status.in_(ACTIVE_CLOSE_STATUSES)
        ).values(status="canceling", detail="kill switch was enabled"))
        db.execute(update(CloseRequestPosition).where(
            CloseRequestPosition.status.in_(ACTIVE_CLOSE_STATUSES)
        ).values(status="canceling", detail="kill switch was enabled"))
        db.execute(update(CloseRequestPosition).where(
            CloseRequestPosition.status == "canceling",
            ~select(Order.id).where(
                Order.close_position_id == CloseRequestPosition.id,
                Order.status.not_in(TERMINAL_ORDER_STATUSES),
            ).exists(),
        ).values(status="canceled"))
        db.execute(update(CloseRequest).where(
            CloseRequest.status == "canceling",
            ~select(Order.id).where(
                Order.close_request_id == CloseRequest.id,
                Order.status.not_in(TERMINAL_ORDER_STATUSES),
            ).exists(),
        ).values(status="canceled"))
    db.commit()
    db.refresh(item)
    return _control_update_view(item, cancellation_requested_count)
