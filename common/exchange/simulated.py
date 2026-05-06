from __future__ import annotations

import random
import time

from common.exchange.base import Exchange
from common.models import BudgetConfig, TradeSignal


class SimulatedExchange(Exchange):
    def __init__(self, budget: BudgetConfig, start_price: float = 100.0):
        self.quote_balance = budget.quote_budget
        self.base_balance = budget.base_budget
        self.initial_quote = budget.quote_budget
        self.initial_base = budget.base_budget
        self.price = start_price

    def get_price(self, fallback_price: float | None = None) -> float:
        move = random.uniform(-0.01, 0.01)
        self.price = max(0.0001, self.price * (1 + move))
        return self.price

    def wait_for_price_update(self, last_price: float | None = None, timeout_seconds: float = 1.0) -> float:
        time.sleep(max(0.05, timeout_seconds))
        return self.get_price(last_price)

    def get_balances(self) -> tuple[float, float]:
        return self.quote_balance, self.base_balance

    def execute(self, signal: TradeSignal, price: float | None = None) -> bool:
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
