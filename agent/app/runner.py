"""Bot runner and lifecycle management for the CryptoBot agent.

Contains the :class:`AgentLogStore` for in-memory log collection,
:class:`BotRunner` which drives the strategy + exchange loop in a
background thread, and :class:`RunnerManager` which owns the runners.
"""
from __future__ import annotations

import threading
import time
import uuid
import hashlib
from datetime import datetime, timezone
import os
import traceback
from typing import Literal
import logging

import requests

from common.diagnostics import debug_log, get_correlation_id, scoped_context, trace_log
from common import (
    BotConfig,
    BotSnapshot,
    BudgetConfig,
    Exchange,
    BitvavoExchange,
    RunnerState,
    SimulatedExchange,
    StrategyState,
    StaticGridStrategy,
    TradeSignal,
)


logger = logging.getLogger(__name__)


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
        # Agent logs are operational only; detailed trading events are
        # exposed via bot trade-events and should not pollute agent logs.
        if category == "trading":
            return

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

        with scoped_context(bot_id=bot_id, component="agent.log_store"):
            debug_log(
                logger,
                f"agent_log_{event_type}",
                message,
                bot_id=bot_id,
                category=category,
                data=data or {},
            )

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

        self.price = getattr(self.exchange, 'price', 0.0)
        self.initial_equity = config.budget.quote_budget + config.budget.base_budget * self.price
        self.realized_pnl = 0.0
        self.skimmed_quote = 0.0
        self.trade_count = 0
        self._pending_trade_events: list[dict] = []
        self.started_at: datetime | None = None
        self.trace_id = get_correlation_id()

    def _wait_for_initial_price(self) -> bool:
        """Block until the exchange provides the first usable price."""
        wait_timeout = 15.0 if self.config.mode == "live" else 5.0
        while self.running:
            try:
                price = self.exchange.wait_for_price_update(self.price or None, timeout_seconds=wait_timeout)
                if price and price > 0:
                    self.price = price
                    return True
            except Exception as exc:
                self.log_store.add(
                    "price_wait_error",
                    f"Waiting for initial price failed: {exc}",
                    bot_id=self.bot_id,
                    category="system",
                )
            time.sleep(0.5)
        return False

    def _build_snapshot(self, status: str) -> BotSnapshot:
        """Create a snapshot for the current bot state."""
        total_equity = self._calculate_total_equity()
        runtime_seconds = 0
        if self.started_at is not None:
            runtime_seconds = max(0, int((datetime.now(timezone.utc) - self.started_at).total_seconds()))
        return BotSnapshot(
            bot_id=self.bot_id,
            runtime_seconds=runtime_seconds,
            timestamp=datetime.now(timezone.utc),
            price=self.price,
            quote_balance=self.exchange.quote_balance,
            base_balance=self.exchange.base_balance,
            base_value_in_quote=self.exchange.base_balance * self.price,
            total_equity_quote=total_equity,
            realized_pnl_quote=self.realized_pnl,
            unrealized_pnl_quote=total_equity - self.initial_equity,
            skimmed_quote=self.skimmed_quote,
            trade_count=self.trade_count,
            status=status,
        )

    def _calculate_total_equity(self) -> float:
        """Return full mark-to-market equity, including reserved live orders when available."""
        if self.trade_count == 0:
            # Keep the startup baseline stable only while balances still match
            # the configured starting base position. If base holdings changed
            # (e.g. restored session or exchange-side buy fill), use
            # mark-to-market immediately.
            base_delta = abs(float(self.exchange.base_balance) - float(self.config.budget.base_budget))
            if base_delta <= 1e-12:
                return self.initial_equity

        total_equity = self.exchange.quote_balance + self.exchange.base_balance * self.price
        if self.config.mode != "live":
            return total_equity

        open_orders = getattr(self.exchange, "_limit_orders", None)
        if not isinstance(open_orders, dict):
            return total_equity

        reserved_quote = 0.0
        reserved_base_value = 0.0
        for order in open_orders.values():
            if not isinstance(order, dict):
                continue
            side = str(order.get("side", "") or "").lower()
            quote_amount = float(order.get("quote_amount") or 0.0)
            limit_price = float(order.get("limit_price") or 0.0)
            if side == "buy":
                reserved_quote += max(0.0, quote_amount)
            elif side == "sell" and limit_price > 0 and self.price > 0:
                reserved_base_value += max(0.0, (quote_amount / limit_price) * self.price)

        return total_equity + reserved_quote + reserved_base_value

    def _bitvavo_operator_id(self) -> int:
        """Derive a stable positive int64 operatorId from the bot identifier."""
        try:
            bot_uuid = uuid.UUID(str(self.bot_id))
            return bot_uuid.int % ((1 << 63) - 1)
        except ValueError:
            digest = hashlib.sha256(str(self.bot_id).encode("utf-8")).digest()
            return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)

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
                missing: list[str] = []
                if not api_key:
                    missing.append("BITVAVO_API_KEY")
                if not api_secret:
                    missing.append("BITVAVO_API_SECRET")
                missing_list = ", ".join(missing)
                raise RuntimeError(
                    f"Missing live-mode credentials: {missing_list}. "
                    "Set them in agent/.env (or an env_file loaded by the agent service)."
                )

            return BitvavoExchange(
                api_key=api_key,
                api_secret=api_secret,
                operator_id=self._bitvavo_operator_id(),
                bot_id=self.bot_id,
                market=config.market,
                base_currency=config.base_currency,
                quote_currency=config.quote_currency,
            )

        # Always use SimulatedExchange with market, which will fetch price from Bitvavo
        return SimulatedExchange(
            config.budget,
            market=config.market,
            fee_rate=float(config.fee_rate or os.getenv("SIM_MAKER_FEE_RATE", os.getenv("SIM_FEE_RATE", "0.0"))),
        )

    def _apply_live_fill_to_virtual_balances(self, fill: dict) -> None:
        """Apply one confirmed live fill to bot-scoped virtual balances."""
        side = str(fill.get("side", "") or "").lower()
        quote_amount = float(fill.get("quote_amount", 0.0) or 0.0)
        fill_price = float(fill.get("fill_price", 0.0) or 0.0)
        fee_paid_quote = float(fill.get("fee_paid_quote", 0.0) or 0.0)

        if quote_amount <= 0 or fill_price <= 0:
            return

        base_amount = quote_amount / fill_price

        if side == "buy":
            self.exchange.quote_balance -= quote_amount
            # Convert fee to base-equivalent so base holdings reflect net position.
            net_base = max(0.0, base_amount - (fee_paid_quote / fill_price if fee_paid_quote > 0 else 0.0))
            self.exchange.base_balance += net_base
        elif side == "sell":
            self.exchange.base_balance = max(0.0, self.exchange.base_balance - base_amount)
            self.exchange.quote_balance += max(0.0, quote_amount - fee_paid_quote)

    def start(self, restored: bool = False) -> None:
        """
        Start the exchange connection and launch the trading loop.

        The heavy work (WS connect, price wait) runs in the background
        thread so the HTTP caller is not blocked.

        :param restored: If True, skip strategy priming (state was restored from failover).
        """
        if self.running:
            return
        self.running = True
        if self.started_at is None:
            self.started_at = datetime.now(timezone.utc)
        self.thread = threading.Thread(target=self._startup_and_loop, args=(restored,), daemon=True)
        self.thread.start()

    def _startup_and_loop(self, restored: bool) -> None:
        """Connect to the exchange, prime the strategy, then enter the tick loop."""
        with scoped_context(
            correlation_id=self.trace_id,
            agent_id=self.agent_id,
            bot_id=self.bot_id,
            component="agent.runner",
        ):
            try:
                self.exchange.start()

                if not restored:
                    if self.config.mode == "live":
                        # Keep bot accounting isolated from full-account balances.
                        self.exchange.quote_balance = float(self.config.budget.quote_budget)
                        self.exchange.base_balance = float(self.config.budget.base_budget)
                    else:
                        quote_balance, base_balance = self.exchange.get_balances()
                        self.exchange.quote_balance = quote_balance
                        self.exchange.base_balance = base_balance

                if not self._wait_for_initial_price():
                    return

                if not restored:
                    self.strategy.on_price(self.price, self.state)
                    self.initial_equity = self.config.budget.quote_budget + self.config.budget.base_budget * self.price
                    synced_levels = self._sync_existing_live_open_orders()
                    if synced_levels:
                        levels_sorted = sorted(synced_levels)
                        levels_text = ", ".join(str(level) for level in levels_sorted)
                        self.log_store.add(
                            "orders_recovered",
                            f"Recovered {len(synced_levels)} existing open order(s) from exchange before seeding. Levels: {levels_text}",
                            bot_id=self.bot_id,
                            data={"levels": levels_sorted},
                            category="system",
                        )
                    # Publish the first running snapshot before submitting initial orders
                    # so the manager UI can leave "initializing" immediately.
                    self._push_snapshot(self._build_snapshot("running"))
                    self._place_all_limit_orders("initial")
                    # Flush initial order_placed events and runner state right away.
                    self._push_snapshot(self._build_snapshot("running"))
                else:
                    if not self.state.open_orders:
                        # Restored but no open orders (e.g. crash during fill) — reinitialize grid
                        self.state.level_index = None
                        self.strategy.on_price(self.price, self.state)

                    synced_levels = self._sync_existing_live_open_orders()
                    if synced_levels:
                        levels_sorted = sorted(synced_levels)
                        levels_text = ", ".join(str(level) for level in levels_sorted)
                        self.log_store.add(
                            "orders_recovered",
                            f"Recovered {len(synced_levels)} existing open order(s) from exchange before restore. Levels: {levels_text}",
                            bot_id=self.bot_id,
                            data={"levels": levels_sorted},
                            category="system",
                        )

                    self._push_snapshot(self._build_snapshot("running"))
                    # Startup reconcile: only post missing levels, never duplicate recovered orders.
                    self._place_ready_open_orders("restore-startup", log_deferred=True)
                    self._push_snapshot(self._build_snapshot("running"))

                label = "resumed" if restored else "started"
                self.log_store.add("bot_start", f"Bot {label}.", bot_id=self.bot_id, category="system")
                if restored:
                    self.log_store.add(
                        "bot_resumed",
                        "Continuing from saved state: "
                        f"level={self.state.level_index}, open_orders={len(self.state.open_orders)}, "
                        f"trades={self.trade_count}, equity={self.initial_equity:.2f}",
                        bot_id=self.bot_id,
                        data={
                            "level_index": self.state.level_index,
                            "open_orders": len(self.state.open_orders),
                            "filled_buys": len(self.state.filled_buys),
                            "trade_count": self.trade_count,
                            "initial_equity": round(self.initial_equity, 4),
                            "price": self.price,
                        },
                        category="system",
                    )

                self._loop()
            except Exception as exc:
                self.running = False
                self.log_store.add(
                    "bot_error",
                    f"Failed to start: {exc}",
                    bot_id=self.bot_id,
                    data={
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                        "traceback": traceback.format_exc(limit=20),
                        "trace_id": self.trace_id,
                    },
                    category="system",
                )
                debug_log(
                    logging.getLogger(__name__),
                    "bot_start_failed",
                    "Bot failed to start in runner thread",
                    bot_id=self.bot_id,
                    agent_id=self.agent_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise

    def stop(self) -> None:
        """
        Stop the trading loop and close the exchange connection.
        """
        self.stop_with_mode(cancel_orders=True)

    def stop_with_mode(self, cancel_orders: bool = True) -> None:
        """Stop the trading loop with optional open-order cancellation."""
        self.running = False
        if cancel_orders:
            self.exchange.cancel_all_orders()
        self.exchange.stop()
        suffix = " (open orders kept)" if not cancel_orders else ""
        self.log_store.add("bot_stop", f"Bot stopped{suffix}.", bot_id=self.bot_id, category="system")

    def prepare_delete(self, delete_mode: Literal["delete_open_orders", "delete_as_is", "transform_to_base", "transform_to_quote"]) -> dict:
        """Execute bot delete preparation on the connected exchange before stopping."""
        self.running = False
        quote_before, base_before = self.exchange.get_balances()
        self.exchange.quote_balance = quote_before
        self.exchange.base_balance = base_before

        details = {
            "mode": delete_mode,
            "quote_before": quote_before,
            "base_before": base_before,
            "actions": [],
        }

        if delete_mode == "delete_open_orders":
            self.exchange.cancel_all_orders()
            details["actions"].append("cancel_open_orders")
            self.exchange.stop()
            self.log_store.add(
                "bot_delete_prepared",
                "Prepared for delete: cancelled open orders and stopped.",
                bot_id=self.bot_id,
                data=details,
                category="system",
            )
            return details

        if delete_mode == "delete_as_is":
            self.exchange.stop()
            self.log_store.add(
                "bot_delete_prepared",
                "Prepared for delete: keeping open orders and balances as-is.",
                bot_id=self.bot_id,
                data=details,
                category="system",
            )
            return details

        self.exchange.cancel_all_orders()
        details["actions"].append("cancel_open_orders")

        price = self.price
        if price <= 0:
            price = self.exchange.get_price(None)
            self.price = price

        if delete_mode == "transform_to_base":
            quote_to_spend = max(self.exchange.quote_balance, 0.0)
            if quote_to_spend > 0:
                ok = self.exchange.execute(TradeSignal(side="buy", quote_amount=quote_to_spend), price=price)
                if not ok:
                    raise RuntimeError("Failed to transform quote balance into base at market price")
                details["actions"].append("buy_base_with_all_quote")
        elif delete_mode == "transform_to_quote":
            base_to_sell = max(self.exchange.base_balance, 0.0)
            quote_to_receive = base_to_sell * price
            if quote_to_receive > 0:
                ok = self.exchange.execute(TradeSignal(side="sell", quote_amount=quote_to_receive), price=price)
                if not ok:
                    raise RuntimeError("Failed to transform base balance into quote at market price")
                details["actions"].append("sell_all_base_to_quote")

        quote_after, base_after = self.exchange.get_balances()
        self.exchange.quote_balance = quote_after
        self.exchange.base_balance = base_after
        details["quote_after"] = quote_after
        details["base_after"] = base_after
        details["price_used"] = price

        self.exchange.stop()
        self.log_store.add(
            "bot_delete_prepared",
            f"Prepared for delete with mode {delete_mode}.",
            bot_id=self.bot_id,
            data=details,
            category="system",
        )
        return details

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
            "Budget updated.",
            bot_id=self.bot_id,
            data={"quote_budget": budget.quote_budget, "base_budget": budget.base_budget},
            category="system",
        )

    def get_runner_state(self) -> RunnerState:
        """Capture the full runner state for persistence / failover."""
        return RunnerState(
            level_index=self.state.level_index,
            open_orders={int(k): v for k, v in self.state.open_orders.items()},
            filled_buys=sorted(self.state.filled_buys),
            filled_amounts={int(k): v for k, v in self.state.filled_amounts.items()},
            started_at=self.started_at,
            price=self.price,
            quote_balance=self.exchange.quote_balance,
            base_balance=self.exchange.base_balance,
            initial_equity=self.initial_equity,
            realized_pnl=self.realized_pnl,
            skimmed_quote=self.skimmed_quote,
            trade_count=self.trade_count,
        )

    def restore_runner_state(self, rs: RunnerState) -> None:
        """Restore runner from a previously saved state."""
        self.state.level_index = rs.level_index
        self.state.open_orders = {int(k): v for k, v in rs.open_orders.items()}
        self.state.filled_buys = set(rs.filled_buys)
        self.state.filled_amounts = {int(k): v for k, v in rs.filled_amounts.items()}
        self.started_at = rs.started_at or self.started_at
        self.price = rs.price
        self.exchange.quote_balance = rs.quote_balance
        self.exchange.base_balance = rs.base_balance
        self.initial_equity = rs.initial_equity
        self.realized_pnl = rs.realized_pnl
        self.skimmed_quote = rs.skimmed_quote
        self.trade_count = rs.trade_count

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
            runner_state = self.get_runner_state()
            events = self._pending_trade_events
            self._pending_trade_events = []
            with scoped_context(
                correlation_id=self.trace_id,
                agent_id=self.agent_id,
                bot_id=self.bot_id,
                component="agent.runner.snapshot",
            ):
                trace_log(
                    logging.getLogger(__name__),
                    "snapshot_push_attempt",
                    "Pushing bot snapshot to manager",
                    bot_id=self.bot_id,
                    agent_id=self.agent_id,
                    trade_events=len(events),
                    status=snapshot.status,
                )
                requests.post(
                    f"{self.manager_url}/api/v1/agents/{self.agent_id}/bots/{self.bot_id}/metrics",
                    json={
                        "snapshot": snapshot.model_dump(mode="json"),
                        "runner_state": runner_state.model_dump(mode="json"),
                        "trade_events": events,
                    },
                    timeout=4,
                    headers={"x-correlation-id": self.trace_id},
                )
                debug_log(
                    logging.getLogger(__name__),
                    "snapshot_push_ok",
                    "Bot snapshot pushed to manager",
                    bot_id=self.bot_id,
                    agent_id=self.agent_id,
                    status=snapshot.status,
                )
        except requests.RequestException as exc:
            debug_log(
                logging.getLogger(__name__),
                "snapshot_push_failed",
                "Bot snapshot push failed",
                bot_id=self.bot_id,
                agent_id=self.agent_id,
                error=str(exc),
            )
            return

    def _sync_existing_live_open_orders(self) -> set[int]:
        """Import already-open exchange orders for planned levels in live mode."""
        if self.config.mode != "live" or not isinstance(self.exchange, BitvavoExchange):
            return set()
        planned_levels = sorted(self.state.open_orders.keys())
        self.log_store.add(
            "orders_sync_planned",
            f"Syncing existing open orders on exchange for {len(planned_levels)} planned level(s).",
            bot_id=self.bot_id,
            data={"levels": planned_levels},
            category="system",
        )
        synced_levels = self.exchange.sync_open_orders_for_levels(
            planned_open_orders=self.state.open_orders,
            level_prices=self.strategy.levels,
            quote_amount=self.config.grid.order_size_quote,
        )
        recovery_details: list[dict] = []
        if hasattr(self.exchange, "get_last_open_order_sync_matches"):
            try:
                recovery_details = list(getattr(self.exchange, "get_last_open_order_sync_matches")() or [])
            except Exception:
                recovery_details = []
        self.log_store.add(
            "orders_sync_completed",
            f"Exchange open-order sync completed: matched {len(synced_levels)} level(s).",
            bot_id=self.bot_id,
            data={"matched_levels": sorted(synced_levels), "matches": recovery_details},
            category="system",
        )
        return synced_levels

    def _limit_order_readiness(self, level_idx: int, side: str, limit_price: float) -> tuple[bool, str | None, dict | None]:
        """Return whether an order is ready to post on the exchange right now."""
        if side == "buy":
            if self.price > 0 and limit_price >= self.price:
                return False, "buy_above_market", {
                    "level_index": level_idx,
                    "side": side,
                    "limit_price": limit_price,
                    "current_price": self.price,
                }
            return True, None, None

        required_base = self.config.grid.order_size_quote / limit_price if limit_price > 0 else 0.0
        if self.exchange.base_balance + 1e-12 < required_base:
            return False, "insufficient_base", {
                "level_index": level_idx,
                "side": side,
                "limit_price": limit_price,
                "required_base": required_base,
                "available_base": self.exchange.base_balance,
            }
        return True, None, None

    def _place_ready_open_orders(self, context: str = "", log_deferred: bool = False) -> None:
        """Try to place currently open strategy orders that are valid to post now."""
        for idx in sorted(self.state.open_orders.keys()):
            side = self.state.open_orders.get(idx)
            if side is None:
                continue
            limit_price = self.strategy.levels[idx]
            if self.exchange.has_tracked_level_order(idx, side, limit_price):
                continue
            ready, reason, details = self._limit_order_readiness(idx, side, limit_price)
            if not ready:
                if log_deferred:
                    self.log_store.add(
                        "order_waiting",
                        f"Waiting to post {side.upper()} at level {idx} (price {limit_price:.6f}): {reason}",
                        bot_id=self.bot_id,
                        data={"context": context, "reason": reason, **(details or {})},
                        category="trading",
                    )
                continue
            self._place_limit_order(idx, context)

    def _place_limit_order(self, level_idx: int, context: str = "") -> None:
        """Place a single limit order on the exchange for a grid level."""
        side = self.state.open_orders.get(level_idx)
        if side is None:
            return
        limit_price = self.strategy.levels[level_idx]
        order_reference = f"level-{level_idx}"
        ready, reason, details = self._limit_order_readiness(level_idx, side, limit_price)
        if not ready:
            self.log_store.add(
                "order_waiting",
                f"Waiting to post {side.upper()} at level {level_idx} (price {limit_price:.6f}): {reason}",
                bot_id=self.bot_id,
                data={"context": context, "reason": reason, **(details or {})},
                category="trading",
            )
            return
        if self.exchange.has_tracked_level_order(level_idx, side, limit_price):
            self.log_store.add(
                "order_already_open",
                f"Skipped duplicate order ({context}): {side.upper()} at level {level_idx} (price {limit_price:.6f})",
                bot_id=self.bot_id,
                data={"side": side, "level_index": level_idx, "price": limit_price, "context": context},
                category="trading",
            )
            return
        order_id = f"{self.bot_id}-{level_idx}-{uuid.uuid4().hex[:12]}"
        self.log_store.add(
            "order_posting",
            f"Posting limit order ({context}): {side.upper()} at level {level_idx} (price {limit_price:.6f})",
            bot_id=self.bot_id,
            data={
                "order_id": order_id,
                "order_reference": order_reference,
                "side": side,
                "level_index": level_idx,
                "price": limit_price,
                "quote_amount": self.config.grid.order_size_quote,
                "context": context,
            },
            category="trading",
        )
        try:
            success = self.exchange.place_limit_order(
                order_id=order_id,
                side=side,
                quote_amount=self.config.grid.order_size_quote,
                limit_price=limit_price,
                level_index=level_idx,
                client_reference=order_reference,
            )
        except Exception as exc:
            self.log_store.add(
                "order_post_failed",
                f"Failed posting limit order ({context}) at level {level_idx}: {exc}",
                bot_id=self.bot_id,
                data={
                    "order_id": order_id,
                    "order_reference": order_reference,
                    "side": side,
                    "level_index": level_idx,
                    "price": limit_price,
                    "quote_amount": self.config.grid.order_size_quote,
                    "context": context,
                    "error": str(exc),
                },
                category="trading",
            )
            raise
        if success:
            self.log_store.add(
                "order_opened",
                f"Limit order ({context}): {side.upper()} at level {level_idx} (price {limit_price:.6f})",
                bot_id=self.bot_id,
                data={"side": side, "level_index": level_idx, "price": limit_price, "context": context},
                category="trading",
            )
            self._pending_trade_events.append({
                "event_type": "order_placed",
                "order_id": order_id,
                "side": side,
                "quote_amount": self.config.grid.order_size_quote,
                "price": limit_price,
                "level_index": level_idx,
                "trade_pnl": 0,
                "total_equity": self.exchange.quote_balance + self.exchange.base_balance * self.price,
                "trade_number": self.trade_count,
            })

    def _place_all_limit_orders(self, context: str = "") -> None:
        """Place limit orders for all current open orders in state."""
        planned: list[dict[str, float | int | str]] = []
        for idx in sorted(self.state.open_orders.keys()):
            side = self.state.open_orders.get(idx)
            if side is None:
                continue
            planned.append({
                "level_index": idx,
                "side": side,
                "price": self.strategy.levels[idx],
                "quote_amount": self.config.grid.order_size_quote,
            })

        self.log_store.add(
            "orders_batch_planned",
            f"Planning to post {len(planned)} limit order(s) ({context or 'n/a'}).",
            bot_id=self.bot_id,
            data={"context": context, "orders": planned},
            category="trading",
        )

        self._place_ready_open_orders(context, log_deferred=True)

    def _loop(self) -> None:
        """Main trading loop: wait for price updates, check for filled limit orders."""
        while self.running:
            try:
                wait_timeout = 15.0 if self.config.mode == "live" else 5.0
                self.price = self.exchange.wait_for_price_update(self.price, timeout_seconds=wait_timeout)
            except Exception as exc:
                self.log_store.add(
                    "price_error",
                    f"Price retrieval failed: {exc}",
                    bot_id=self.bot_id,
                    category="system",
                )
                time.sleep(0.5)
                continue

            # Process fills — loop until no more cascading fills
            while True:
                fills = self.exchange.get_filled_orders()
                if self.config.mode == "live" and isinstance(self.exchange, BitvavoExchange):
                    try:
                        reconciled_fills = self.exchange.reconcile_planned_level_orders(
                            planned_open_orders=self.state.open_orders,
                            level_prices=self.strategy.levels,
                            quote_amount=self.config.grid.order_size_quote,
                        )
                    except Exception:
                        reconciled_fills = []
                    if reconciled_fills:
                        fills.extend(reconciled_fills)
                if not fills:
                    break

                for fill in fills:
                    idx = fill["level_index"]
                    side = fill["side"]
                    fill_price = fill["fill_price"]
                    fill_count = int(fill.get("fill_count", 0) or 0)
                    fill_parts_suffix = f" in {fill_count} part(s)" if fill_count > 0 else ""

                    # Remove from strategy state
                    self.state.open_orders.pop(idx, None)

                    self.log_store.add(
                        "order_filled",
                        f"Filled: {side.upper()} {fill['quote_amount']:.2f} at level {idx} (price {fill_price:.6f}){fill_parts_suffix}",
                        bot_id=self.bot_id,
                        data={
                            "side": side,
                            "quote_amount": fill["quote_amount"],
                            "level_index": idx,
                            "fill_price": fill_price,
                            "fill_count": fill_count,
                        },
                        category="trading",
                    )

                    signal = TradeSignal(
                        side=side,
                        quote_amount=fill["quote_amount"],
                        level_index=idx,
                    )

                    before_equity = self._calculate_total_equity()

                    # Confirm the fill — places follow-up orders in state
                    orders_before = set(self.state.open_orders.keys())
                    self.strategy.confirm_fill(signal, self.state)

                    # Update balances
                    if self.config.mode == "live" and isinstance(self.exchange, BitvavoExchange):
                        self._apply_live_fill_to_virtual_balances(fill)
                    else:
                        quote_balance, base_balance = self.exchange.get_balances()
                        self.exchange.quote_balance = quote_balance
                        self.exchange.base_balance = base_balance
                    after_equity = self._calculate_total_equity()
                    trade_pnl = after_equity - before_equity
                    self.realized_pnl += trade_pnl
                    self.trade_count += 1

                    self.log_store.add(
                        "trade_executed",
                        f"{side.upper()} {fill['quote_amount']:.2f} at {fill_price:.6f}{fill_parts_suffix} | pnl: {trade_pnl:+.4f}",
                        bot_id=self.bot_id,
                        data={
                            "side": side,
                            "quote_amount": fill["quote_amount"],
                            "fill_count": fill_count,
                            "price": fill_price,
                            "trade_pnl_quote": round(trade_pnl, 6),
                            "realized_pnl_quote": round(self.realized_pnl, 6),
                            "trade_number": self.trade_count,
                        },
                        category="trading",
                    )
                    self._pending_trade_events.append({
                        "event_type": "order_filled",
                        "order_id": fill.get("order_id"),
                        "side": side,
                        "quote_amount": fill["quote_amount"],
                        "fill_count": fill_count,
                        "fee_paid_quote": round(float(fill.get("fee_paid_quote", 0.0) or 0.0), 8),
                        "fee_rate": round(float(fill.get("fee_rate", 0.0) or 0.0), 8),
                        "price": fill_price,
                        "level_index": idx,
                        "trade_pnl": round(trade_pnl, 6),
                        "total_equity": round(self._calculate_total_equity(), 4),
                        "trade_number": self.trade_count,
                    })

                    # Place any new follow-up orders only after balances reflect the fill.
                    for new_idx in sorted(self.state.open_orders.keys()):
                        if new_idx not in orders_before:
                            self._place_limit_order(new_idx, "follow-up")

            if self.config.mode != "live":
                quote_balance, base_balance = self.exchange.get_balances()
                self.exchange.quote_balance = quote_balance
                self.exchange.base_balance = base_balance

            total_equity = self._calculate_total_equity()
            self._apply_profit_mode(total_equity)
            total_equity = self._calculate_total_equity()

            # Re-check waiting orders as market price / balances change.
            self._place_ready_open_orders("pending")

            snapshot = self._build_snapshot("running")
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

    def start_bot(self, bot_id: str, config: BotConfig, runner_state: RunnerState | None = None) -> None:
        """
        Start a bot, creating a new runner if needed.

        :param bot_id: Unique identifier for the bot.
        :param config: Full bot configuration.
        :param runner_state: Optional saved state for failover resume.
        """
        if bot_id in self.runners:
            self.runners[bot_id].start()
            return
        runner = BotRunner(bot_id, config, self.manager_url, self.agent_id, self.log_store)
        if runner_state:
            runner.restore_runner_state(runner_state)
        self.runners[bot_id] = runner
        runner.start(restored=runner_state is not None)

    def stop_bot(self, bot_id: str, cancel_orders: bool = True) -> None:
        """
        Stop a running bot.

        :param bot_id: Unique identifier of the bot to stop.
        """
        runner = self.runners.get(bot_id)
        if not runner:
            return
        runner.stop_with_mode(cancel_orders=cancel_orders)

    def prepare_delete(
        self,
        bot_id: str,
        delete_mode: Literal["delete_open_orders", "delete_as_is", "transform_to_base", "transform_to_quote"],
    ) -> dict:
        """Run delete preparation mode for a running bot."""
        runner = self.runners.get(bot_id)
        if not runner:
            raise RuntimeError("Bot is not running on this agent")
        details = runner.prepare_delete(delete_mode)
        self.runners.pop(bot_id, None)
        return details

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
