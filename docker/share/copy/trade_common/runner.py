from __future__ import annotations

import logging
import hashlib
import signal
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from sqlalchemy.orm import Session
from sqlalchemy import func, select, update

from .config import ExchangeSettings, Settings
from .close_service import append_child, refresh_parent, refresh_target
from .database import session_factory
from .exchange_adapters import ExchangeAdapterPool
from .market_rules import MarketRuleError, TERMINAL_ORDER_STATUSES, fresh_mark_price, positive_decimal
from .models import CloseRequest, CloseRequestPosition, ControlFlag, Fill, Order, Position
from .repository import claim_order, heartbeat, record_fill
from .risk import evaluate_order, validate_instrument_market
from .simulation import simulate_order
from .valuation import ValuationError, order_book_midpoint


LOGGER = logging.getLogger(__name__)


class PaperExecutor:
    def __init__(
        self,
        config: Settings,
        *,
        adapters: ExchangeAdapterPool | None = None,
        sessions: Callable[[], Session] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.config = config
        self.service = "paper-executor"
        self.adapters = adapters or ExchangeAdapterPool(config)
        self.sessions = sessions or session_factory()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.running = True

    def stop(self, *_args):
        self.running = False

    def run(self):
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        LOGGER.info("paper executor started: exchanges=%s", ",".join(self.config.exchanges))
        while self.running:
            try:
                self.process_once()
                time.sleep(self.config.poll_interval_seconds)
            except Exception:
                LOGGER.exception("executor loop failed")
                with self.sessions() as session:
                    heartbeat(session, self.service, healthy=False, detail="executor loop failed")
                    session.commit()
                time.sleep(max(self.config.poll_interval_seconds, 2.0))
        try:
            self.adapters.close()
        except Exception:
            LOGGER.warning("exchange adapter pool close failed", exc_info=True)
        LOGGER.info("paper executor stopped")

    def process_once(self) -> bool:
        with self.sessions() as session:
            heartbeat(session, self.service)
            order = claim_order(session, now=self.now())
            session.commit()
        if order is None:
            return False
        self._process(order.id)
        return True

    def _process(self, order_id: str):
        with self.sessions() as snapshot_session:
            snapshot = snapshot_session.get(Order, order_id)
            if snapshot is None or snapshot.status in TERMINAL_ORDER_STATUSES:
                return
            exchange_id, symbol = snapshot.exchange_id, snapshot.symbol
            parent_id, target_id = snapshot.close_request_id, snapshot.close_position_id
            reduce_only, order_type = snapshot.reduce_only, snapshot.order_type
            control_snapshot = snapshot_session.get(ControlFlag, 1)
            skip_public_data = bool(snapshot.cancellation_requested or (control_snapshot and control_snapshot.kill_switch))

        # Fetch public data without holding the rows that serialize fills and
        # trading controls. The transaction below rechecks every mutable state.
        exchange_config = self.config.exchange(exchange_id)
        instrument = book = None
        mark_price = mark_observed_at = None
        external_error = None
        external_stage = "instrument metadata"
        if not skip_public_data and exchange_config is not None and (exchange_id == "bybit" or symbol in exchange_config.symbols):
            try:
                adapter = self.adapters.get(exchange_id)
                instruments = adapter.fetch_instruments(symbol)
                if not instruments:
                    raise RuntimeError("instrument metadata is unavailable")
                instrument = instruments[0]
                supported_bybit = (
                    instrument.get("contract_type") == "linear_perpetual"
                    and instrument.get("quote_asset") == "USDT"
                    and instrument.get("settle_asset") == "USDT"
                )
                needs_mark = exchange_id == "bybit" and supported_bybit and (
                    (instrument.get("status") == "active" and parent_id is None)
                    or (instrument.get("status") != "active" and reduce_only and order_type == "market")
                )
                if needs_mark:
                    external_stage = "Mark Price"
                    mark_price, mark_observed_at = fresh_mark_price(adapter, symbol)
                if exchange_id != "bybit" or (supported_bybit and instrument.get("status") == "active"):
                    external_stage = "market data"
                    book = adapter.fetch_order_book(symbol)
            except Exception as exc:
                external_error = exc
        with self.sessions() as session:
            control = session.get(ControlFlag, 1, with_for_update=True)
            if control is None:
                control = ControlFlag(id=1)
                session.add(control)
                session.flush()
            position = session.scalar(select(Position).where(
                Position.exchange_id == exchange_id, Position.symbol == symbol
            ).with_for_update())
            parent = session.get(CloseRequest, parent_id, with_for_update=True) if parent_id else None
            target = session.get(CloseRequestPosition, target_id, with_for_update=True) if target_id else None
            order = session.get(Order, order_id, with_for_update=True)
            if order is None or order.status in TERMINAL_ORDER_STATUSES:
                return
            if control.kill_switch or order.cancellation_requested or (parent is not None and parent.status in {"canceling", "canceled", "failed", "completed"}):
                order.status = "canceled"
                order.rejection_reason = "kill switch was enabled" if control.kill_switch else "order cancellation was requested"
                self._after_close_child(session, order, parent, target, None)
                session.commit()
                return

            if exchange_config is None:
                self._reject(order, "configured exchange is unavailable")
                self._fail_close_target(session, parent, target, "exchange_not_configured", "configured exchange is unavailable")
                session.commit()
                return
            if order.exchange_id != "bybit" and order.symbol not in exchange_config.symbols:
                self._reject(order, "symbol is not allowed for exchange")
                self._fail_close_target(session, parent, target, "unsupported_market", "symbol is not allowed for exchange")
                session.commit()
                return

            LOGGER.info(
                "processing paper order: order_id=%s exchange=%s symbol=%s",
                order.id,
                order.exchange_id,
                order.symbol,
            )
            if external_error is not None:
                if isinstance(external_error, MarketRuleError) and external_error.status_code == 422:
                    self._reject(order, external_error.detail)
                    self._fail_close_target(session, parent, target, external_error.reason_code, external_error.detail)
                else:
                    reason = external_error.detail if isinstance(external_error, MarketRuleError) else f"{external_stage} is unavailable"
                    reason_code = external_error.reason_code if isinstance(external_error, MarketRuleError) else (
                        "instrument_data_unavailable" if external_stage == "instrument metadata"
                        else "mark_price_unavailable" if external_stage == "Mark Price"
                        else "order_book_unavailable"
                    )
                    LOGGER.warning("%s: order_id=%s error=%s", reason, order.id, external_error)
                    self._defer(order, reason)
                    self._after_close_child(session, order, parent, target, instrument, reason_code)
                session.commit()
                return
            if instrument is None:
                self._defer(order, "instrument metadata is unavailable")
                self._after_close_child(session, order, parent, target, None, "instrument_data_unavailable")
                session.commit()
                return
            remaining = Decimal(order.quantity) - Decimal(order.filled_quantity)
            market_decision = validate_instrument_market(
                instrument, exchange_id=order.exchange_id, order_type=order.order_type,
                reduce_only=order.reduce_only,
                position_quantity=Decimal(position.quantity) if position is not None else Decimal("0"),
                side=order.side, quantity=remaining,
            )
            if not market_decision.allowed:
                if market_decision.temporary:
                    self._defer(order, market_decision.reason or "instrument metadata is unavailable")
                    self._after_close_child(session, order, parent, target, instrument, market_decision.reason_code)
                elif order.close_request_id is not None and market_decision.reason and market_decision.reason.startswith("reduce-only"):
                    order.status = "canceled"
                    order.rejection_reason = "position changed before child execution"
                    self._after_close_child(session, order, parent, target, instrument)
                elif order.exchange_id == "bybit" and instrument.get("status") != "active" and order.reduce_only and order.order_type == "limit":
                    order.status = "canceled"
                    order.rejection_reason = "Reduce-only limit canceled after instrument stopped trading"
                    self._after_close_child(session, order, parent, target, instrument)
                else:
                    self._reject(order, market_decision.reason or "market is not tradable")
                    if market_decision.reason_code == "unsupported_market":
                        self._fail_close_target(session, parent, target, market_decision.reason_code, market_decision.reason)
                    else:
                        self._after_close_child(session, order, parent, target, instrument)
                session.commit()
                return

            mark_only = order.exchange_id == "bybit" and instrument.get("status") != "active" and order.reduce_only and order.order_type == "market"
            if order.exchange_id == "bybit" and (mark_only or order.close_request_id is None) and mark_price is None:
                self._defer(order, "fresh Mark Price is unavailable")
                self._after_close_child(session, order, parent, target, instrument, "mark_price_unavailable")
                session.commit()
                return
            if mark_only:
                decision = evaluate_order(
                    session, order, mark_price, self.config, exchange_config,
                    instrument, execution_price=mark_price, valuation_price=mark_price,
                )
                if self._defer_if_mark_stale(session, order, parent, target, instrument, mark_price, mark_observed_at):
                    session.commit()
                    return
                if not decision.allowed:
                    self._handle_decision(session, order, parent, target, instrument, decision)
                else:
                    self._reset_retry(order)
                    self._paper_execute_mark(session, order, mark_price, mark_observed_at, exchange_config)
                    self._after_close_child(session, order, parent, target, instrument)
                session.commit()
                return
            if book is None:
                self._defer(order, "market data is unavailable")
                self._after_close_child(session, order, parent, target, instrument)
                session.commit()
                return

            try:
                mid_price = self._validate_market_age(book)
            except (RuntimeError, ValuationError) as exc:
                self._defer(order, str(exc))
                self._after_close_child(session, order, parent, target, instrument)
                session.commit()
                return
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                self._defer(order, "order book is empty")
                self._after_close_child(session, order, parent, target, instrument)
                session.commit()
                return
            book_id = str(book.get("_market_data_id") or "")
            if target is not None and book_id and session.scalar(select(Order.id).where(
                Order.close_position_id == target.id,
                Order.close_sequence < order.close_sequence,
                Order.last_market_data_id == book_id,
                Order.filled_quantity > 0,
            ).limit(1)) is not None:
                self._defer(order, "waiting for a new order book snapshot")
                self._after_close_child(session, order, parent, target, instrument)
                session.commit()
                return
            best_bid = Decimal(str(bids[0][0]))
            best_ask = Decimal(str(asks[0][0]))
            execution_price = best_ask if order.side == "buy" else best_bid
            decision = evaluate_order(
                session,
                order,
                mid_price,
                self.config,
                exchange_config,
                instrument,
                execution_price=execution_price,
                valuation_price=mark_price,
            )
            if self._defer_if_mark_stale(session, order, parent, target, instrument, mark_price, mark_observed_at):
                session.commit()
                return
            if not decision.allowed:
                self._handle_decision(session, order, parent, target, instrument, decision)
                session.commit()
                return

            self._reset_retry(order)
            self._paper_execute(session, order, book, exchange_config)
            self._after_close_child(session, order, parent, target, instrument)
            session.commit()

    def _handle_decision(self, session, order, parent, target, instrument, decision):
        if decision.temporary:
            self._defer(order, decision.reason or "instrument metadata is unavailable")
        elif target is not None and decision.reason and decision.reason.startswith("reduce-only"):
            order.status = "canceled"
            order.rejection_reason = "position changed before child execution"
        else:
            self._reject(order, decision.reason or "risk check rejected")
        self._after_close_child(session, order, parent, target, instrument, decision.reason_code)

    def _fail_close_target(self, session, parent, target, reason_code, detail):
        if target is None or parent is None:
            return
        target.status = "failed"
        target.reason_code = reason_code
        target.detail = detail
        session.execute(update(Order).where(
            Order.close_position_id == target.id,
            Order.status.not_in(TERMINAL_ORDER_STATUSES),
        ).values(cancellation_requested=True))
        refresh_parent(session, parent)

    def _after_close_child(self, session, order, parent, target, instrument, wait_reason_code=None):
        if target is None or parent is None:
            return
        session.flush()
        refresh_target(session, target)
        if order.status not in TERMINAL_ORDER_STATUSES:
            if order.rejection_reason:
                target.status = "waiting"
                target.reason_code = wait_reason_code or "order_book_unavailable"
                target.detail = order.rejection_reason
            refresh_parent(session, parent)
            return
        if parent.status not in {"canceling", "canceled", "completed", "failed"} and target.status not in {"failed", "canceled"}:
            remaining = Decimal(target.remaining_position_quantity)
            if remaining and remaining * Decimal(target.initial_position_quantity) < 0:
                self._fail_close_target(session, parent, target, "position_reversed", "position direction changed")
                return
            if remaining == 0:
                session.execute(update(Order).where(
                    Order.close_position_id == target.id,
                    Order.status.not_in(TERMINAL_ORDER_STATUSES),
                    Order.cancellation_requested.is_(False),
                ).values(cancellation_requested=True))
            elif instrument is not None:
                scheduled = Decimal(session.scalar(select(func.coalesce(
                    func.sum(Order.quantity - Order.filled_quantity), 0,
                )).where(
                    Order.close_position_id == target.id,
                    Order.status.not_in(TERMINAL_ORDER_STATUSES),
                    Order.cancellation_requested.is_(False),
                )) or 0)
                needed = abs(remaining) - scheduled
                if needed > 0:
                    cap = needed
                    if order.exchange_id == "bybit":
                        cap = min(cap, positive_decimal(instrument.get("max_market_qty")) or needed)
                    if instrument.get("max_qty") is not None:
                        cap = min(cap, positive_decimal(instrument.get("max_qty")) or needed)
                    while needed > 0:
                        chunk = min(needed, cap)
                        append_child(session, parent, target, quantity=chunk, network=self.config.exchange_network)
                        needed -= chunk
                        if parent.generation_mode == "incremental":
                            break
                refresh_target(session, target)
        refresh_parent(session, parent)

    def _paper_execute_mark(self, session, order, price, observed_at, exchange_config):
        remaining = Decimal(order.quantity) - Decimal(order.filled_quantity)
        if not self._allow_fill_notional(session, order, remaining * price):
            return
        identity = hashlib.sha256(f"{order.symbol}:{price}:{observed_at.isoformat()}".encode()).hexdigest()
        record_fill(
            session, order, quantity=remaining, price=price,
            fee=remaining * price * exchange_config.taker_fee_rate,
            liquidity_role="taker", market_data_id=identity,
            executed_at=self.now(),
        )
        order.last_market_data_id = identity
        order.status = "filled"
        order.rejection_reason = None

    def _validate_market_age(self, book: dict) -> Decimal:
        price, _ = order_book_midpoint(
            book, now=self.now(),
            max_age_seconds=self.config.market_data_max_age_seconds,
        )
        return price

    def _defer_if_mark_stale(self, session, order, parent, target, instrument, mark_price, observed_at) -> bool:
        if mark_price is None:
            return False
        if observed_at is None:
            reason = "Mark Price has no timestamp"
        else:
            age_seconds = (self.now() - observed_at).total_seconds()
            if age_seconds > self.config.market_data_max_age_seconds:
                reason = "Mark Price is stale"
            elif age_seconds < -2:
                reason = "Mark Price timestamp is too far in the future"
            else:
                return False
        self._defer(order, reason)
        self._after_close_child(session, order, parent, target, instrument, "mark_price_unavailable")
        return True

    def _paper_execute(
        self,
        session: Session,
        order: Order,
        book: dict,
        exchange_config: ExchangeSettings,
    ):
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        market_data_id = str(book.get("_market_data_id") or "")
        if not market_data_id:
            self._defer(order, "market data has no identity")
            return
        if order.last_market_data_id == market_data_id:
            if order.close_request_id is not None:
                self._defer(order, "waiting for a new order book snapshot")
            else:
                self._set_waiting_status(order)
            return

        remaining = Decimal(order.quantity) - Decimal(order.filled_quantity)
        if remaining <= 0:
            order.status = "filled"
            return

        was_resting = order.resting_since is not None
        if order.order_type == "limit":
            limit_price = Decimal(order.limit_price)
            best_opposite = Decimal(str(asks[0][0] if order.side == "buy" else bids[0][0]))
            marketable = best_opposite <= limit_price if order.side == "buy" else best_opposite >= limit_price
            if not marketable:
                order.last_market_data_id = market_data_id
                order.resting_since = order.resting_since or self.now()
                order.rejection_reason = None
                self._set_waiting_status(order)
                return

        liquidity_role = "taker" if order.order_type == "market" or not was_resting else "maker"
        fee_rate = (
            exchange_config.taker_fee_rate
            if liquidity_role == "taker"
            else exchange_config.maker_fee_rate
        )
        result = simulate_order(
            side=order.side,
            order_type=order.order_type,
            quantity=remaining,
            limit_price=Decimal(order.limit_price) if order.limit_price is not None else None,
            bids=bids,
            asks=asks,
            fee_rate=fee_rate,
        )
        order.last_market_data_id = market_data_id
        order.rejection_reason = None
        if result is None:
            if order.order_type == "limit":
                order.resting_since = order.resting_since or self.now()
                self._set_waiting_status(order)
            else:
                self._defer(order, "insufficient market liquidity")
            return
        if not self._allow_fill_notional(session, order, result.notional):
            return
        record_fill(
            session,
            order,
            quantity=result.quantity,
            price=result.average_price,
            fee=result.fee,
            liquidity_role=liquidity_role,
            market_data_id=market_data_id,
            executed_at=self.now(),
        )
        if result.fully_filled:
            order.status = "filled"
        elif order.order_type == "limit":
            order.resting_since = order.resting_since or self.now()
            self._set_waiting_status(order)
        else:
            order.status = "canceled"
            order.rejection_reason = "market order partially filled; unfilled quantity canceled"

    def _allow_fill_notional(self, session: Session, order: Order, proposed_notional: Decimal) -> bool:
        control = session.get(ControlFlag, 1)
        if order.close_request_id is not None or (control is not None and control.close_only and order.reduce_only):
            return True
        prior_notional = sum(
            (Decimal(quantity) * Decimal(price) for quantity, price in session.execute(
                select(Fill.quantity, Fill.price).where(Fill.order_id == order.id)
            )),
            Decimal("0"),
        )
        if prior_notional + proposed_notional <= self.config.max_order_notional:
            return True
        if Decimal(order.filled_quantity) > 0:
            order.status = "canceled"
            order.rejection_reason = "maximum cumulative order notional exceeded; remaining quantity canceled"
        else:
            self._reject(order, "maximum cumulative order notional exceeded")
        return False

    @staticmethod
    def _reject(order: Order, reason: str):
        order.status = "rejected"
        order.rejection_reason = reason

    def _set_waiting_status(self, order: Order):
        order.status = "partially_filled" if Decimal(order.filled_quantity) > 0 else "open"
        order.next_attempt_at = self.now() + timedelta(seconds=self.config.poll_interval_seconds)

    def _defer(self, order: Order, reason: str):
        order.retry_count = int(order.retry_count or 0) + 1
        delay_seconds = min(60, 2 ** min(order.retry_count, 6))
        order.next_attempt_at = self.now() + timedelta(seconds=delay_seconds)
        if Decimal(order.filled_quantity) > 0:
            order.status = "partially_filled"
        elif order.order_type == "limit" and order.resting_since is not None:
            order.status = "open"
        else:
            order.status = "pending"
        order.rejection_reason = reason

    def _reset_retry(self, order: Order) -> None:
        order.retry_count = 0
        order.next_attempt_at = self.now()
