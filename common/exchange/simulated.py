from __future__ import annotations

from common.exchange.base import Exchange
from common.models import BudgetConfig, TradeSignal


class SimulatedExchange(Exchange):
    def __init__(self, budget: BudgetConfig):
        self.quote_balance = budget.quote_budget
        self.base_balance = budget.base_budget
        self.initial_quote = budget.quote_budget
        self.initial_base = budget.base_budget

    def execute(self, signal: TradeSignal, price: float) -> bool:
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
