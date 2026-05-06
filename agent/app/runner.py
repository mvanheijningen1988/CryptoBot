from __future__ import annotations

import random
import threading
import time
from datetime import datetime, timezone

import requests

from common.exchange.simulated import SimulatedExchange
from common.models import BotConfig, BotSnapshot, BudgetConfig
from common.strategy.base import StrategyState
from common.strategy.static_grid import StaticGridStrategy


class BotRunner:
    def __init__(self, bot_id: str, config: BotConfig, manager_url: str, agent_id: str):
        self.bot_id = bot_id
        self.config = config
        self.manager_url = manager_url.rstrip("/")
        self.agent_id = agent_id
        self.running = False
        self.thread: threading.Thread | None = None

        self.exchange = SimulatedExchange(config.budget)
        self.strategy = StaticGridStrategy(config.grid)
        self.state = StrategyState()

        self.price = config.start_price
        self.initial_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price
        self.realized_pnl = 0.0
        self.skimmed_quote = 0.0

    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False

    def update_budget(self, budget: BudgetConfig):
        self.exchange.quote_balance = budget.quote_budget
        self.exchange.base_balance = budget.base_budget

    def _apply_profit_mode(self, total_equity: float):
        if self.config.budget.profit_mode != "skim":
            return
        profit = total_equity - self.initial_equity
        if profit <= 0:
            return
        skim = profit * self.config.budget.skim_ratio
        if skim > 0 and self.exchange.quote_balance >= skim:
            self.exchange.quote_balance -= skim
            self.skimmed_quote += skim
            self.initial_equity += skim

    def _push_snapshot(self, snapshot: BotSnapshot):
        try:
            requests.post(
                f"{self.manager_url}/api/agents/{self.agent_id}/bots/{self.bot_id}/metrics",
                json={"snapshot": snapshot.model_dump(mode="json")},
                timeout=4,
            )
        except requests.RequestException:
            return

    def _loop(self):
        while self.running:
            move = random.uniform(-0.01, 0.01)
            self.price = max(0.0001, self.price * (1 + move))

            signals = self.strategy.on_price(self.price, self.state)
            for signal in signals:
                before_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price
                success = self.exchange.execute(signal, self.price)
                if success:
                    after_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price
                    self.realized_pnl += after_equity - before_equity

            total_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price
            self._apply_profit_mode(total_equity)
            total_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price

            snapshot = BotSnapshot(
                bot_id=self.bot_id,
                timestamp=datetime.now(timezone.utc),
                price=self.price,
                quote_balance=self.exchange.quote_balance,
                base_balance=self.exchange.base_balance,
                base_value_in_quote=self.exchange.base_balance * self.price,
                total_equity_quote=total_equity,
                realized_pnl_quote=self.realized_pnl,
                unrealized_pnl_quote=total_equity - self.initial_equity,
                skimmed_quote=self.skimmed_quote,
                status="running",
            )
            self._push_snapshot(snapshot)
            time.sleep(self.config.tick_seconds)


class RunnerManager:
    def __init__(self, manager_url: str, agent_id: str):
        self.manager_url = manager_url
        self.agent_id = agent_id
        self.runners: dict[str, BotRunner] = {}

    def start_bot(self, bot_id: str, config: BotConfig):
        if bot_id in self.runners:
            self.runners[bot_id].start()
            return
        runner = BotRunner(bot_id, config, self.manager_url, self.agent_id)
        self.runners[bot_id] = runner
        runner.start()

    def stop_bot(self, bot_id: str):
        runner = self.runners.get(bot_id)
        if not runner:
            return
        runner.stop()

    def update_budget(self, bot_id: str, budget: BudgetConfig):
        runner = self.runners.get(bot_id)
        if not runner:
            return
        runner.update_budget(budget)

    def list_bots(self):
        return [{"bot_id": bot_id, "running": runner.running} for bot_id, runner in self.runners.items()]
