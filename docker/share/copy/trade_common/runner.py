from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable

from sqlalchemy.orm import Session

from .config import ExchangeSettings, Settings
from .database import session_factory
from .exchange_adapters import ExchangeAdapterPool
from .models import Order
from .repository import claim_order, heartbeat, record_fill
from .risk import evaluate_order
from .simulation import simulate_order


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
        with self.sessions() as session:
            order = session.get(Order, order_id)
            if order is None:
                return
            if order.cancellation_requested:
                order.status = "canceled"
                session.commit()
                return

            exchange_config = self.config.exchange(order.exchange_id)
            if exchange_config is None:
                self._reject(order, "configured exchange is unavailable")
                session.commit()
                return
            if order.symbol not in exchange_config.symbols:
                self._reject(order, "symbol is not allowed for exchange")
                session.commit()
                return

            LOGGER.info(
                "processing paper order: order_id=%s exchange=%s symbol=%s",
                order.id,
                order.exchange_id,
                order.symbol,
            )
            try:
                adapter = self.adapters.get(order.exchange_id)
                instruments = adapter.fetch_instruments(order.symbol)
                if not instruments:
                    raise RuntimeError("instrument metadata is unavailable")
                instrument = instruments[0]
            except Exception:
                LOGGER.warning(
                    "instrument metadata unavailable: order_id=%s exchange=%s symbol=%s",
                    order.id,
                    order.exchange_id,
                    order.symbol,
                    exc_info=True,
                )
                self._defer(order, "instrument metadata is unavailable")
                session.commit()
                return
            try:
                book = adapter.fetch_order_book(order.symbol)
            except Exception:
                LOGGER.warning(
                    "market data unavailable: order_id=%s exchange=%s symbol=%s",
                    order.id,
                    order.exchange_id,
                    order.symbol,
                    exc_info=True,
                )
                self._defer(order, "market data is unavailable")
                session.commit()
                return

            try:
                self._validate_market_age(book)
            except RuntimeError as exc:
                self._defer(order, str(exc))
                session.commit()
                return
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                self._defer(order, "order book is empty")
                session.commit()
                return
            self._reset_retry(order)
            best_bid = Decimal(str(bids[0][0]))
            best_ask = Decimal(str(asks[0][0]))
            mid_price = (best_bid + best_ask) / Decimal("2")
            execution_price = best_ask if order.side == "buy" else best_bid
            decision = evaluate_order(
                session,
                order,
                mid_price,
                self.config,
                exchange_config,
                instrument,
                execution_price=execution_price,
            )
            if not decision.allowed:
                self._reject(order, decision.reason or "risk check rejected")
                session.commit()
                return

            self._paper_execute(session, order, book, exchange_config)
            session.commit()

    def _validate_market_age(self, book: dict):
        duration = float(book.get("_request_duration_seconds") or 0)
        if duration > self.config.market_data_max_age_seconds:
            raise RuntimeError(f"market data request was too slow: duration={duration:.3f}s")

        timestamp_ms = book.get("timestamp")
        if timestamp_ms:
            observed_at = datetime.fromtimestamp(float(timestamp_ms) / 1000, tz=timezone.utc)
        else:
            observed_at = book.get("_received_at")
            if isinstance(observed_at, str):
                observed_at = datetime.fromisoformat(observed_at)
            if not isinstance(observed_at, datetime):
                raise RuntimeError("market data has no usable timestamp")
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)

        age = (self.now() - observed_at).total_seconds()
        if age > self.config.market_data_max_age_seconds:
            raise RuntimeError(f"market data is stale: age={age:.3f}s")

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
                self._reject(order, "insufficient market liquidity")
            return
        record_fill(
            session,
            order,
            quantity=result.quantity,
            price=result.average_price,
            fee=result.fee,
            liquidity_role=liquidity_role,
            market_data_id=market_data_id,
        )
        if result.fully_filled:
            order.status = "filled"
        elif order.order_type == "limit":
            order.resting_since = order.resting_since or self.now()
            self._set_waiting_status(order)
        else:
            order.status = "canceled"
            order.rejection_reason = "market order partially filled; unfilled quantity canceled"

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
