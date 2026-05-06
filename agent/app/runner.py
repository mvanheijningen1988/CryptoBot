"""Bot runner and lifecycle management for the CryptoBot agent.

Contains the :class:`AgentLogStore` for in-memory log collection,
:class:`BotRunner` which drives the strategy + exchange loop in a
background thread, and :class:`RunnerManager` which owns the runners.
"""
from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
import os

import requests

from common.exchange.base import Exchange
from common.exchange.bitvavo import BitvavoExchange
from common.exchange.simulated import SimulatedExchange
from common.models import BotConfig, BotSnapshot, BudgetConfig
from common.strategy.base import StrategyState
from common.strategy.static_grid import StaticGridStrategy


class AgentLogStore:
    """Thread-safe, bounded, in-memory log buffer for agent events."""

    def __init__(self, max_logs: int = 2000) -> None:
        """
        Create a log store that keeps at most max_logs entries.

        :param max_logs: Maximum number of log entries to retain.
        """
        self.max_logs = max_logs
        self.logs: list[dict] = []
        self.lock = threading.Lock()

    def add(
        self,
        event_type: str,
        message: str,
        bot_id: str | None = None,
        data: dict | None = None,
        category: str = "system",
    ) -> None:
        """
        Append a log entry, evicting the oldest if the buffer is full.

        :param event_type: Short identifier for the event (e.g. 'trade_executed').
        :param message: Human-readable description of the event.
        :param bot_id: Optional bot ID this event relates to.
        :param data: Optional extra data dict attached to the entry.
        :param category: Log category ('system' or 'trading').
        """
        item = {
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "category": category,
            "event_type": event_type,
            "bot_id": bot_id,
            "message": message,
            "data": data or {},
        }
        with self.lock:
            self.logs.insert(0, item)
            if len(self.logs) > self.max_logs:
                del self.logs[self.max_logs :]

    def get(self, limit: int = 200, bot_id: str | None = None, category: str | None = None) -> list[dict]:
        """
        Return the most recent log entries, optionally filtered.

        :param limit: Maximum number of entries to return.
        :param bot_id: Optional filter by bot ID.
        :param category: Optional filter by log category.
        :return: List of log entry dicts, newest first.
        """
        with self.lock:
            logs = self.logs
            if bot_id:
                logs = [x for x in logs if x.get("bot_id") == bot_id]
            if category:
                logs = [x for x in logs if x.get("category") == category]
            return logs[:limit]


class BotRunner:
    """Runs a single bot's strategy loop in a background thread.

    Wires together an :class:`Exchange` and a :class:`StaticGridStrategy`,
    executing signals and pushing snapshots to the manager.
    """

    def __init__(self, bot_id: str, config: BotConfig, manager_url: str, agent_id: str, log_store: AgentLogStore) -> None:
        """
        Initialise the runner, building the exchange and strategy from config.

        :param bot_id: Unique identifier for the bot.
        :param config: Full bot configuration (market, grid, budget, mode).
        :param manager_url: Base URL of the manager service.
        :param agent_id: ID of the agent running this bot.
        :param log_store: Shared log store for recording events.
        """
        self.bot_id = bot_id
        self.config = config
        self.manager_url = manager_url.rstrip("/")
        self.agent_id = agent_id
        self.log_store = log_store
        self.running = False
        self.thread: threading.Thread | None = None

        self.exchange = self._build_exchange(config)
        self.strategy = StaticGridStrategy(config.grid)
        self.state = StrategyState()

        self.price = config.start_price
        self.initial_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price
        self.realized_pnl = 0.0
        self.skimmed_quote = 0.0

    def _build_exchange(self, config: BotConfig) -> Exchange:
        """
        Create the appropriate exchange adapter based on config.mode.

        :param config: Bot configuration specifying mode and credentials.
        :return: An Exchange instance (SimulatedExchange or BitvavoExchange).
        :raises RuntimeError: If live mode credentials are missing or provider unsupported.
        """
        if config.mode == "live":
            provider = os.getenv("LIVE_EXCHANGE_PROVIDER", "bitvavo").lower()
            if provider != "bitvavo":
                raise RuntimeError(f"Unsupported live exchange provider: {provider}")

            api_key = os.getenv("BITVAVO_API_KEY", "")
            api_secret = os.getenv("BITVAVO_API_SECRET", "")
            if not api_key or not api_secret:
                raise RuntimeError("BITVAVO_API_KEY and BITVAVO_API_SECRET are required for live mode")

            return BitvavoExchange(
                api_key=api_key,
                api_secret=api_secret,
                market=config.market,
                base_currency=config.base_currency,
                quote_currency=config.quote_currency,
            )

        return SimulatedExchange(config.budget, start_price=config.start_price)

    def start(self) -> None:
        """
        Start the exchange connection and launch the trading loop.
        """
        if self.running:
            return
        self.running = True
        self.exchange.start()
        quote_balance, base_balance = self.exchange.get_balances()
        self.exchange.quote_balance = quote_balance
        self.exchange.base_balance = base_balance

        for _ in range(20):
            try:
                self.price = self.exchange.get_price(self.price)
                break
            except Exception:
                time.sleep(0.5)

        self.initial_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price
        self.log_store.add("bot_start", f"Bot {self.bot_id} started.", bot_id=self.bot_id, category="system")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """
        Stop the trading loop and close the exchange connection.
        """
        self.running = False
        self.exchange.stop()
        self.log_store.add("bot_stop", f"Bot {self.bot_id} stopped.", bot_id=self.bot_id, category="system")

    def update_budget(self, budget: BudgetConfig) -> None:
        """
        Hot-swap the exchange balances for simulation mode.

        :param budget: New budget with quote and base amounts.
        """
        if isinstance(self.exchange, SimulatedExchange):
            self.exchange.quote_balance = budget.quote_budget
            self.exchange.base_balance = budget.base_budget
        self.log_store.add(
            "budget_update",
            f"Budget updated for bot {self.bot_id}.",
            bot_id=self.bot_id,
            data={"quote_budget": budget.quote_budget, "base_budget": budget.base_budget},
            category="system",
        )

    def _apply_profit_mode(self, total_equity: float) -> None:
        """
        Apply skim profit mode if configured (simulation only).

        :param total_equity: Current total equity in quote currency.
        """
        if self.config.mode != "simulation":
            return
        mode = self.config.budget.profit_mode
        if mode == "withdraw":
            return
        if mode == "compound":
            return
        if mode != "skim":
            return
        profit = total_equity - self.initial_equity
        if profit <= 0:
            return
        skim = profit * self.config.budget.skim_ratio
        if skim > 0 and self.exchange.quote_balance >= skim:
            self.exchange.quote_balance -= skim
            self.skimmed_quote += skim
            self.initial_equity += skim

    def _push_snapshot(self, snapshot: BotSnapshot) -> None:
        """
        Send a performance snapshot to the manager.

        :param snapshot: Point-in-time bot metrics to push.
        """
        try:
            requests.post(
                f"{self.manager_url}/api/agents/{self.agent_id}/bots/{self.bot_id}/metrics",
                json={"snapshot": snapshot.model_dump(mode="json")},
                timeout=4,
            )
        except requests.RequestException:
            return

    def _loop(self) -> None:
        """
        Main trading loop: fetch price, run strategy, execute signals, push metrics.
        """
        while self.running:
            try:
                wait_timeout = 15.0 if self.config.mode == "live" else 1.0
                self.price = self.exchange.wait_for_price_update(self.price, timeout_seconds=wait_timeout)
            except Exception as exc:
                self.log_store.add(
                    "price_error",
                    f"Price retrieval failed for bot {self.bot_id}: {exc}",
                    bot_id=self.bot_id,
                    category="system",
                )
                time.sleep(0.5)
                continue

            signals = self.strategy.on_price(self.price, self.state)
            for signal in signals:
                before_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price
                try:
                    success = self.exchange.execute(signal, self.price)
                except Exception as exc:
                    success = False
                    self.log_store.add(
                        "trade_error",
                        f"Trade failed for bot {self.bot_id}: {exc}",
                        bot_id=self.bot_id,
                        category="system",
                    )
                if success:
                    quote_balance, base_balance = self.exchange.get_balances()
                    self.exchange.quote_balance = quote_balance
                    self.exchange.base_balance = base_balance
                    after_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price
                    self.realized_pnl += after_equity - before_equity
                    self.log_store.add(
                        "trade_executed",
                        f"{signal.side.upper()} executed for bot {self.bot_id} at price {self.price:.6f}",
                        bot_id=self.bot_id,
                        data={
                            "side": signal.side,
                            "quote_amount": signal.quote_amount,
                            "price": self.price,
                            "realized_pnl_quote": self.realized_pnl,
                        },
                        category="trading",
                    )
                else:
                    self.log_store.add(
                        "trade_skipped",
                        f"{signal.side.upper()} skipped for bot {self.bot_id} due to balance/risk constraints.",
                        bot_id=self.bot_id,
                        data={"side": signal.side, "quote_amount": signal.quote_amount, "price": self.price},
                        category="trading",
                    )

            quote_balance, base_balance = self.exchange.get_balances()
            self.exchange.quote_balance = quote_balance
            self.exchange.base_balance = base_balance

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


class RunnerManager:
    """Manages the collection of :class:`BotRunner` instances on this agent."""

    def __init__(self, manager_url: str, agent_id: str) -> None:
        """
        Create a runner manager for the given agent.

        :param manager_url: Base URL of the manager service.
        :param agent_id: Unique identifier for this agent.
        """
        self.manager_url = manager_url
        self.agent_id = agent_id
        self.runners: dict[str, BotRunner] = {}
        self.log_store = AgentLogStore()

    def start_bot(self, bot_id: str, config: BotConfig) -> None:
        """
        Start a bot, creating a new runner if needed.

        :param bot_id: Unique identifier for the bot.
        :param config: Full bot configuration.
        """
        if bot_id in self.runners:
            self.runners[bot_id].start()
            return
        runner = BotRunner(bot_id, config, self.manager_url, self.agent_id, self.log_store)
        self.runners[bot_id] = runner
        runner.start()

    def stop_bot(self, bot_id: str) -> None:
        """
        Stop a running bot.

        :param bot_id: Unique identifier of the bot to stop.
        """
        runner = self.runners.get(bot_id)
        if not runner:
            return
        runner.stop()

    def update_budget(self, bot_id: str, budget: BudgetConfig) -> None:
        """
        Update the budget of a running bot.

        :param bot_id: Unique identifier of the bot.
        :param budget: New budget with quote and base amounts.
        """
        runner = self.runners.get(bot_id)
        if not runner:
            return
        runner.update_budget(budget)

    def list_bots(self) -> list[dict]:
        """
        Return a list of all managed bot IDs and their running status.

        :return: List of dicts with 'bot_id' and 'running' keys.
        """
        return [{"bot_id": bot_id, "running": runner.running} for bot_id, runner in self.runners.items()]

    def get_logs(self, limit: int = 200, bot_id: str | None = None, category: str | None = None) -> list[dict]:
        """
        Retrieve recent logs, optionally filtered by bot_id or category.

        :param limit: Maximum number of entries to return.
        :param bot_id: Optional filter by bot ID.
        :param category: Optional filter by log category.
        :return: List of log entry dicts, newest first.
        """
        return self.log_store.get(limit=limit, bot_id=bot_id, category=category)

    def log_system(self, event_type: str, message: str, data: dict | None = None) -> None:
        """
        Write a system-level log entry.

        :param event_type: Short identifier for the event.
        :param message: Human-readable description.
        :param data: Optional extra data dict.
        """
        self.log_store.add(event_type=event_type, message=message, data=data, category="system")
