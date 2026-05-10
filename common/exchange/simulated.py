"""Simulated exchange for paper trading and back-testing.

Maintains in-memory quote and base balances and executes trades
instantly at the current market price.  When a *market* symbol is
provided, real prices are streamed from the Bitvavo **public**
WebSocket (no authentication, no real orders); otherwise a simple
random walk is used (back-test mode).

Unlike the live exchange, the simulated exchange always fills orders
in full and allows the balance to go negative (virtual budget).
A configurable fee rate is applied to every trade, matching real
exchange behaviour.

**Safety guarantee:** this class never authenticates with Bitvavo and
never sends order actions — all trade execution is pure arithmetic on
in-memory balances.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid

import requests as _requests
import websocket as _ws

from common.exchange.base import Exchange
from common.models import BudgetConfig, TradeSignal

_BITVAVO_TICKER_URL = "https://api.bitvavo.com/v2/ticker/price"
_BITVAVO_WS_URL = "wss://ws.bitvavo.com/v2/"

logger = logging.getLogger(__name__)


class SimulatedExchange(Exchange):
    """In-memory exchange that executes trades at the current market price.

    **Simulation vs live differences:**

    * Orders are always filled instantly and in full (no partial fills).
    * Balances may go negative — the bot operates on a virtual budget.
    * A fee is deducted from every trade (configurable via *fee_rate*).
    * Prices are streamed from the Bitvavo **public** WebSocket when
      *market* is set (no API key required, no orders placed).
    * Falls back to a random walk when no market is configured (back-test).
    """

    def __init__(
        self,
        budget: BudgetConfig,
        market: str | None = None,
        fee_rate: float = 0.0,
    ) -> None:
        """
        Initialise the simulated exchange.

        :param budget: Capital allocation with quote and base amounts.
        :param market: Bitvavo market symbol (e.g. ``'BTC-EUR'``).  When set,
                       prices are streamed from the public WebSocket.
        :param fee_rate: Fee fraction applied per trade (e.g. 0.0025 = 0.25 %).
        """
        self.quote_balance: float = budget.quote_budget
        self.base_balance: float = budget.base_budget
        self.initial_quote: float = budget.quote_budget
        self.initial_base: float = budget.base_budget
        self.price: float = 0.0
        self.market: str | None = market
        self.fee_rate: float = fee_rate

        # Limit order tracking
        self._pending_orders: dict[str, dict] = {}
        self._fills: list[dict] = []

        # WebSocket state (only used when market is set)
        self._ws: _ws.WebSocket | None = None
        self._ws_thread: threading.Thread | None = None
        self._running = False
        self._price_event = threading.Event()

    # ── Connection lifecycle ──────────────────────────────────

    def start(self) -> None:
        """Open the public Bitvavo WebSocket and subscribe to ticker updates.

        No authentication is performed — only the public ticker channel
        is used.  This method is a no-op when no *market* is configured.
        Blocks briefly until the first real price arrives so that callers
        never see the seed ``start_price``.
        """
        if not self.market or self._running:
            return
        try:
            self._ws = _ws.create_connection(_BITVAVO_WS_URL, timeout=10)
            self._running = True
            self._ws.send(json.dumps({
                "action": "subscribe",
                "channels": [{"name": "ticker", "markets": [self.market]}],
            }))
            self._ws_thread = threading.Thread(target=self._reader_loop, daemon=True)
            self._ws_thread.start()
            logger.info("Simulation WS connected for %s", self.market)
            # Wait up to 10 s for the first real price from the WS
            if self._price_event.wait(timeout=10):
                logger.info("Simulation WS got initial price %.6f for %s", self.price, self.market)
            else:
                logger.warning("Simulation WS did not receive initial price for %s within 10 s", self.market)
        except Exception:
            logger.warning("Simulation WS connect failed for %s, falling back to REST", self.market)
            self._running = False
            self._ws = None

    def stop(self) -> None:
        """Close the WebSocket connection."""
        self._running = False
        if self._ws:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None

    def _reader_loop(self) -> None:
        """Background thread: read ticker messages and update the price."""
        while self._running and self._ws:
            try:
                raw = self._ws.recv()
                if not raw:
                    continue
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    continue
                price = self._extract_price(msg)
                if price is not None:
                    self.price = price
                    self._price_event.set()
            except Exception:
                break
        self._running = False
        logger.info("Simulation WS reader stopped for %s", self.market)

    @staticmethod
    def _extract_price(msg: dict) -> float | None:
        """Try to pull a price from a WS message."""
        for key in ("price", "last"):
            if key in msg:
                try:
                    return float(msg[key])
                except (TypeError, ValueError):
                    pass
        bid, ask = msg.get("bestBid"), msg.get("bestAsk")
        if bid is not None and ask is not None:
            try:
                return (float(bid) + float(ask)) / 2.0
            except (TypeError, ValueError):
                pass
        return None

    # ── Price retrieval ───────────────────────────────────────

    def _fetch_rest_price(self) -> float | None:
        """Fetch the latest price from the Bitvavo public REST API (fallback).

        :return: The current market price, or ``None`` on failure.
        """
        if not self.market:
            return None
        try:
            resp = _requests.get(
                _BITVAVO_TICKER_URL,
                params={"market": self.market},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                return float(data["price"])
        except Exception:
            pass
        return None

    def get_price(self, fallback_price: float | None = None) -> float:
        """Return the current market price.

        When the WebSocket is connected the cached price is returned
        immediately.  If the WS is down but a *market* is configured,
        the REST ticker API is tried as a fallback.  Without a market,
        a simple random walk is applied (back-test mode).

        :param fallback_price: Ignored; present for interface compatibility.
        :return: The current price.
        """
        # WS is feeding prices — just return the latest
        if self._running:
            return self.price

        # WS not available — try REST
        rest = self._fetch_rest_price()
        if rest is not None:
            self.price = rest
            return self.price

        # No market at all — random walk for back-test
        import random

        move = random.uniform(-0.01, 0.01)
        self.price = max(0.0001, self.price * (1 + move))
        return self.price

    def wait_for_price_update(self, last_price: float | None = None, timeout_seconds: float = 1.0) -> float:
        """Block until a new WebSocket price arrives or the timeout elapses.

        Falls back to :meth:`get_price` when WebSocket is not active.

        :param last_price: The previous price (used for change detection on WS).
        :param timeout_seconds: Seconds to wait before giving up.
        :return: The next price.
        """
        if self._running:
            # If the price already changed, return immediately
            if self.price != last_price:
                return self.price
            self._price_event.clear()
            self._price_event.wait(timeout=timeout_seconds)
            return self.price

        # Fallback: sleep + poll
        time.sleep(max(0.05, timeout_seconds))
        return self.get_price(last_price)

    # ── Balance queries ───────────────────────────────────────

    def get_balances(self) -> tuple[float, float]:
        """Return ``(quote_balance, base_balance)``."""
        return self.quote_balance, self.base_balance

    # ── Order execution ───────────────────────────────────────

    def execute(self, signal: TradeSignal, price: float | None = None) -> bool:
        """Execute a buy or sell at the given price with fee deduction.

        Unlike the live exchange, the simulated exchange:

        * Always fills the full order (no partial fills).
        * Allows balances to go negative (virtual budget).
        * Applies :attr:`fee_rate` to every trade.

        :param signal: The trade signal (side + quote_amount).
        :param price: Execution price; defaults to :attr:`price`.
        :return: Always ``True`` (orders always fill in simulation).
        """
        if price is None:
            price = self.price

        fee_multiplier = 1.0 - self.fee_rate

        if signal.side == "buy":
            cost = signal.quote_amount
            base_bought = (cost / price) * fee_multiplier
            self.quote_balance -= cost
            self.base_balance += base_bought
            return True

        if signal.side == "sell":
            base_to_sell = signal.quote_amount / price
            quote_received = (base_to_sell * price) * fee_multiplier
            self.base_balance -= base_to_sell
            self.quote_balance += quote_received
            return True

        return False

    # ── Limit orders ──────────────────────────────────────────

    def place_limit_order(
        self,
        order_id: str,
        side: str,
        quote_amount: float,
        limit_price: float,
        level_index: int | None = None,
        client_reference: str | None = None,
    ) -> bool:
        """Place a pending limit order.

        The order is stored and will fill when the market price reaches
        the limit price (buy ≤ limit, sell ≥ limit).
        """
        self._pending_orders[order_id] = {
            "order_id": order_id,
            "exchange_order_id": str(uuid.uuid4()),
            "status": "new",
            "side": side,
            "quote_amount": quote_amount,
            "limit_price": limit_price,
            "level_index": level_index,
            "client_reference": client_reference,
        }
        return True

    def get_filled_orders(self) -> list[dict]:
        """Check pending orders against the current price and return fills.

        Buy orders fill when ``price ≤ limit_price``.
        Sell orders fill when ``price ≥ limit_price``.
        Balance changes are applied at the limit price (not market price).
        """
        filled: list[dict] = []
        to_remove: list[str] = []
        fee_multiplier = 1.0 - self.fee_rate

        for order_id, order in self._pending_orders.items():
            side = order["side"]
            limit_price = order["limit_price"]
            hit = (side == "buy" and self.price <= limit_price) or \
                  (side == "sell" and self.price >= limit_price)
            if not hit:
                continue

            # Execute at the limit price
            if side == "buy":
                cost = order["quote_amount"]
                base_bought = (cost / limit_price) * fee_multiplier
                fee_paid_quote = cost * self.fee_rate
                self.quote_balance -= cost
                self.base_balance += base_bought
            else:
                base_to_sell = order["quote_amount"] / limit_price
                quote_received = (base_to_sell * limit_price) * fee_multiplier
                fee_paid_quote = order["quote_amount"] * self.fee_rate
                self.base_balance -= base_to_sell
                self.quote_balance += quote_received

            filled.append({
                "order_id": order_id,
                "exchange_order_id": order.get("exchange_order_id"),
                "status": "filled",
                "side": side,
                "quote_amount": order["quote_amount"],
                "fill_price": limit_price,
                "base_amount": base_bought if side == "buy" else base_to_sell,
                "level_index": order["level_index"],
                "fee_paid_quote": fee_paid_quote,
                "fee_rate": self.fee_rate,
            })
            to_remove.append(order_id)

        for oid in to_remove:
            del self._pending_orders[oid]

        return filled

    def cancel_all_orders(self) -> None:
        """Cancel all pending limit orders."""
        self._pending_orders.clear()
