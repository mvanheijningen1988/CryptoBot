"""Simulated exchange for back-testing and paper trading.

Maintains in-memory quote and base balances and executes trades
instantly at the supplied price without fees or slippage.
"""
from __future__ import annotations

import random
import time

from common.exchange.base import Exchange
from common.models import BudgetConfig, TradeSignal


class SimulatedExchange(Exchange):
    """In-memory exchange that executes trades at the given price.

    Used by the back-tester and by bots running in ``simulation`` mode.
    Price evolves via a small random walk each time :meth:`get_price` is
    called.
    """

    def __init__(self, budget: BudgetConfig, start_price: float = 100.0) -> None:
        """
        Initialise balances from a budget config and seed the price.

        :param budget: Capital allocation with quote and base amounts.
        :param start_price: The initial simulated market price.
        """
        self.quote_balance = budget.quote_budget
        self.base_balance = budget.base_budget
        self.initial_quote = budget.quote_budget
        self.initial_base = budget.base_budget
        self.price = start_price

    def get_price(self, fallback_price: float | None = None) -> float:
        """
        Return a randomly-walked price, always > 0.

        :param fallback_price: Unused; included for interface compatibility.
        :return: The new simulated price after a random walk step.
        """
        move = random.uniform(-0.01, 0.01)
        self.price = max(0.0001, self.price * (1 + move))
        return self.price

    def wait_for_price_update(self, last_price: float | None = None, timeout_seconds: float = 1.0) -> float:
        """
        Sleep briefly then return the next simulated price.

        :param last_price: The previous price (unused in simulation).
        :param timeout_seconds: Duration to sleep before returning a new price.
        :return: The next simulated price.
        """
        time.sleep(max(0.05, timeout_seconds))
        return self.get_price(last_price)

    def get_balances(self) -> tuple[float, float]:
        """Return ``(quote_balance, base_balance)``."""
        return self.quote_balance, self.base_balance

    def execute(self, signal: TradeSignal, price: float | None = None) -> bool:
        """
        Execute a buy or sell at the given price.

        :param signal: The trade signal describing side and quote amount.
        :param price: The execution price; uses internal price if None.
        :return: True if executed, False if insufficient balance.
        """
        if price is None:
            price = self.price

        if signal.side == "buy":
            required_quote = signal.quote_amount
            if self.quote_balance < required_quote:
                return False
            base_bought = required_quote / price
            self.quote_balance -= required_quote
            self.base_balance += base_bought
            return True

        if signal.side == "sell":
            base_to_sell = signal.quote_amount / price
            if self.base_balance < base_to_sell:
                return False
            quote_received = base_to_sell * price
            self.base_balance -= base_to_sell
            self.quote_balance += quote_received
            return True

        return False
