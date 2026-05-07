"""Simulated exchange for paper trading and back-testing.

Maintains in-memory quote and base balances and executes trades
instantly at the current market price.  When a *market* symbol is
provided, real prices are fetched from the Bitvavo public REST API;
otherwise a simple random walk is used (back-test mode).

Unlike the live exchange, the simulated exchange always fills orders
in full and allows the balance to go negative (virtual budget).
A configurable fee rate is applied to every trade, matching real
exchange behaviour.
"""
from __future__ import annotations

import time

import requests as _requests

from common.exchange.base import Exchange
from common.models import BudgetConfig, TradeSignal

_BITVAVO_TICKER_URL = "https://api.bitvavo.com/v2/ticker/price"


class SimulatedExchange(Exchange):
    """In-memory exchange that executes trades at the current market price.

    **Simulation vs live differences:**

    * Orders are always filled instantly and in full (no partial fills).
    * Balances may go negative — the bot operates on a virtual budget.
    * A fee is deducted from every trade (configurable via *fee_rate*).
    * Prices come from the Bitvavo public ticker when *market* is set,
      or from a random walk when it is not (back-test).
    """

    def __init__(
        self,
        budget: BudgetConfig,
        start_price: float = 100.0,
        market: str | None = None,
        fee_rate: float = 0.0025,
    ) -> None:
        """
        Initialise the simulated exchange.

        :param budget: Capital allocation with quote and base amounts.
        :param start_price: Seed price used until the first live price arrives.
        :param market: Bitvavo market symbol (e.g. ``'BTC-EUR'``).  When set,
                       :meth:`get_price` fetches the real market price via
                       the public ticker API.
        :param fee_rate: Fee fraction applied per trade (e.g. 0.0025 = 0.25 %).
        """
        self.quote_balance: float = budget.quote_budget
        self.base_balance: float = budget.base_budget
        self.initial_quote: float = budget.quote_budget
        self.initial_base: float = budget.base_budget
        self.price: float = start_price
        self.market: str | None = market
        self.fee_rate: float = fee_rate

    # ── Price retrieval ───────────────────────────────────────

    def _fetch_live_price(self) -> float | None:
        """Fetch the latest price from the Bitvavo public ticker API.

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

        When a *market* is configured the price is fetched from the
        Bitvavo public API.  Otherwise a small random-walk step is
        applied (used by the back-tester).

        :param fallback_price: Ignored; present for interface compatibility.
        :return: The current price.
        """
        live = self._fetch_live_price()
        if live is not None:
            self.price = live
            return self.price
        # Fallback: random walk for back-test mode (no market set)
        import random

        move = random.uniform(-0.01, 0.01)
        self.price = max(0.0001, self.price * (1 + move))
        return self.price

    def wait_for_price_update(self, last_price: float | None = None, timeout_seconds: float = 1.0) -> float:
        """Sleep for *timeout_seconds* then return the next price.

        :param last_price: The previous price (unused).
        :param timeout_seconds: Seconds to sleep before fetching a new price.
        :return: The next price.
        """
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
