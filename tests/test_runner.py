from __future__ import annotations

import pytest
import requests

from common import BotConfig, BudgetConfig, GridConfig
from agent.app.runner import AgentLogStore, BotRunner, RunnerManager
from common.exchange.simulated import SimulatedExchange
from common.exchange.bitvavo import BitvavoExchange


class _StubExchange:
    def __init__(self, prices: list[float]) -> None:
        self._prices = list(prices)
        self.quote_balance = 1000.0
        self.base_balance = 0.0
        self.placed_orders: list[dict] = []

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def cancel_all_orders(self) -> None:
        return None

    def get_balances(self) -> tuple[float, float]:
        return self.quote_balance, self.base_balance

    def wait_for_price_update(self, last_price: float | None = None, timeout_seconds: float = 5.0) -> float:
        if self._prices:
            return self._prices.pop(0)
        return last_price or 0.0

    def place_limit_order(
        self,
        order_id: str,
        side: str,
        quote_amount: float,
        limit_price: float,
        level_index: int | None = None,
        client_reference: str | None = None,
    ) -> bool:
        self.placed_orders.append({
            "order_id": order_id,
            "client_reference": client_reference,
            "side": side,
            "quote_amount": quote_amount,
            "limit_price": limit_price,
            "level_index": level_index,
        })
        return True

    def has_tracked_level_order(self, level_index: int, side: str, limit_price: float) -> bool:  # noqa: ARG002
        return False

    def sync_open_orders_for_levels(
        self,
        planned_open_orders: dict[int, str],
        level_prices: list[float],
        quote_amount: float,
    ) -> set[int]:  # noqa: ARG002
        return set()

    def get_filled_orders(self) -> list[dict]:
        return []


def _config() -> BotConfig:
    return BotConfig(
        market="BTC-EUR",
        base_currency="BTC",
        quote_currency="EUR",
        mode="simulation",
        strategy="static_grid",
        fee_rate=0.0015,
        grid=GridConfig(lower_price=90.0, upper_price=110.0, levels=5, order_size_quote=100.0),
        budget=BudgetConfig(quote_budget=1000.0, base_budget=0.0),
    )


def test_runner_waits_for_real_price_before_initial_orders(monkeypatch):
    exchange = _StubExchange([0.0, 100.0])
    snapshots = []

    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)
    monkeypatch.setattr(BotRunner, "_loop", lambda self: None)
    monkeypatch.setattr(
        BotRunner,
        "_push_snapshot",
        lambda self, snapshot: snapshots.append(
            {
                "snapshot": snapshot,
                "placed_orders": len(exchange.placed_orders),
                "pending_events": len(self._pending_trade_events),
            }
        ),
    )
    monkeypatch.setattr("agent.app.runner.time.sleep", lambda _: None)

    runner = BotRunner("bot-1", _config(), "http://manager:8000", "agent-1", AgentLogStore())
    runner.running = True

    runner._startup_and_loop(restored=False)

    assert [order["level_index"] for order in exchange.placed_orders] == [0, 1]
    assert all(order["side"] == "buy" for order in exchange.placed_orders)
    assert len(snapshots) == 2
    assert snapshots[0]["snapshot"].status == "running"
    assert snapshots[0]["placed_orders"] == 0
    assert snapshots[1]["snapshot"].status == "running"
    assert snapshots[1]["placed_orders"] == 2
    assert snapshots[1]["pending_events"] == 2
    assert runner.price == pytest.approx(100.0)
    event_types = [entry.get("event_type") for entry in runner.log_store.logs]
    assert "bot_start" in event_types
    assert "orders_batch_planned" not in event_types
    assert "order_posting" not in event_types
    assert "order_opened" not in event_types


def test_build_exchange_uses_configured_simulation_fee_rate():
    runner = BotRunner("bot-1", _config(), "http://manager:8000", "agent-1", AgentLogStore())

    exchange = runner._build_exchange(_config())

    assert isinstance(exchange, SimulatedExchange)
    assert exchange.fee_rate == pytest.approx(0.0015)


def test_build_exchange_derives_bitvavo_operator_id_from_bot_id(monkeypatch):
    config = _config().model_copy(update={"mode": "live"})
    monkeypatch.setenv("LIVE_EXCHANGE_PROVIDER", "bitvavo")
    monkeypatch.setenv("BITVAVO_API_KEY", "key")
    monkeypatch.setenv("BITVAVO_API_SECRET", "secret")

    bot_id = "36fe4c40-f2d4-42e0-8408-0b6da0fb8c68"
    runner = BotRunner(bot_id, config, "http://manager:8000", "agent-1", AgentLogStore())

    exchange = runner._build_exchange(config)

    assert isinstance(exchange, BitvavoExchange)
    assert exchange.operator_id == 8215871869382038057


def test_runner_only_posts_buy_orders_below_current_price(monkeypatch):
    exchange = _StubExchange([100.0])

    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)

    runner = BotRunner("bot-buy-filter", _config(), "http://manager:8000", "agent-1", AgentLogStore())
    runner.price = 100.0
    runner.state.open_orders = {0: "buy", 3: "buy"}

    runner._place_all_limit_orders("initial")

    assert [order["level_index"] for order in exchange.placed_orders] == [0]
    waiting_logs = [entry for entry in runner.log_store.logs if entry.get("event_type") == "order_waiting"]
    assert waiting_logs == []


def test_runner_waits_with_sell_orders_until_enough_base(monkeypatch):
    exchange = _StubExchange([100.0])

    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)

    runner = BotRunner("bot-sell-filter", _config(), "http://manager:8000", "agent-1", AgentLogStore())
    runner.price = 100.0
    runner.state.open_orders = {4: "sell"}

    exchange.base_balance = 0.0
    runner._place_all_limit_orders("follow-up")
    assert exchange.placed_orders == []

    exchange.base_balance = 1.0
    runner._place_ready_open_orders("pending")

    assert [order["level_index"] for order in exchange.placed_orders] == [4]
    waiting_logs = [entry for entry in runner.log_store.logs if entry.get("event_type") == "order_waiting"]
    assert waiting_logs == []


def test_quote_amount_for_new_buy_order_only_compounds_in_compound_mode(monkeypatch):
    exchange = _StubExchange([100.0])
    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)

    cfg = _config().model_copy(update={"budget": BudgetConfig(quote_budget=1000.0, base_budget=0.0, profit_mode="withdraw", skim_ratio=0.5)})
    runner = BotRunner("bot-no-compound", cfg, "http://manager:8000", "agent-1", AgentLogStore())
    runner.realized_pnl = 250.0

    amount = runner._quote_amount_for_new_order("buy", 0, 90.0)

    assert amount == pytest.approx(100.0)


def test_live_compound_updates_existing_open_buy_orders(monkeypatch):
    class _LiveCompoundExchange(_StubExchange):
        def __init__(self) -> None:
            super().__init__([100.0])
            self._limit_orders = {
                "order-1": {
                    "side": "buy",
                    "quote_amount": 100.0,
                    "limit_price": 90.0,
                    "level_index": 0,
                    "client_reference": "level-0",
                    "client_order_id": "cid-1",
                    "exchange_order_id": "ex-1",
                }
            }
            self.updated_orders: list[dict] = []

        def update_limit_order(self, order_id: str, quote_amount: float, limit_price: float) -> bool:
            self.updated_orders.append(
                {
                    "order_id": order_id,
                    "quote_amount": quote_amount,
                    "limit_price": limit_price,
                }
            )
            self._limit_orders[order_id]["quote_amount"] = quote_amount
            self._limit_orders[order_id]["limit_price"] = limit_price
            return True

    exchange = _LiveCompoundExchange()
    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)

    cfg = _config().model_copy(
        update={
            "mode": "live",
            "budget": BudgetConfig(quote_budget=1000.0, base_budget=0.0, profit_mode="compound", skim_ratio=0.5),
        }
    )
    runner = BotRunner("bot-compound-update", cfg, "http://manager:8000", "agent-1", AgentLogStore())
    runner.price = 100.0
    runner.realized_pnl = 200.0
    runner.state.open_orders = {0: "buy"}

    updated = runner._update_compound_open_buy_orders("test")

    assert updated == 1
    assert len(exchange.updated_orders) == 1
    assert exchange.updated_orders[0]["order_id"] == "order-1"
    assert exchange.updated_orders[0]["quote_amount"] == pytest.approx(120.0)


def test_compound_update_not_triggered_on_partial_sell_fill(monkeypatch):
    class _PartialSellExchange(_StubExchange):
        def __init__(self) -> None:
            super().__init__([100.0])
            self._fills = [
                {
                    "order_id": "sell-1",
                    "side": "sell",
                    "quote_amount": 10.0,
                    "fill_price": 100.0,
                    "level_index": 0,
                    "fill_count": 1,
                    "fee_paid_quote": 0.0,
                    "fee_rate": 0.0,
                    "order_status": "partiallyFilled",
                }
            ]

        def get_filled_orders(self) -> list[dict]:
            if self._fills:
                return [self._fills.pop(0)]
            return []

    exchange = _PartialSellExchange()
    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)

    cfg = _config().model_copy(
        update={
            "mode": "live",
            "budget": BudgetConfig(quote_budget=1000.0, base_budget=0.0, profit_mode="compound", skim_ratio=0.5),
        }
    )
    runner = BotRunner("bot-partial-sell", cfg, "http://manager:8000", "agent-1", AgentLogStore())
    runner.running = True
    runner.price = 100.0
    runner.state.open_orders = {0: "sell"}

    calls: list[str] = []
    monkeypatch.setattr(BotRunner, "_update_compound_open_buy_orders", lambda self, context="": calls.append(context) or 0)
    monkeypatch.setattr(BotRunner, "_push_snapshot", lambda self, snapshot: setattr(self, "running", False))

    runner._loop()

    assert calls == []


def test_runner_removes_exchange_cancelled_order_before_pending_repost(monkeypatch):
    class _CancelledExchange(_StubExchange):
        def __init__(self) -> None:
            super().__init__([100.0])
            self._cancelled_orders = [{
                "order_id": "cancel-1",
                "exchange_order_id": "ex-1",
                "client_order_id": "client-1",
                "side": "buy",
                "status": "canceled",
                "quote_amount": 100.0,
                "price": 90.0,
                "level_index": 0,
            }]

        def get_cancelled_orders(self) -> list[dict]:
            cancelled_orders = list(self._cancelled_orders)
            self._cancelled_orders.clear()
            return cancelled_orders

    exchange = _CancelledExchange()

    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)

    runner = BotRunner("bot-cancel-sync", _config(), "http://manager:8000", "agent-1", AgentLogStore())
    runner.price = 100.0
    runner.state.open_orders = {0: "buy"}

    removed_count = runner._process_cancelled_orders("pending-sync")
    runner._place_ready_open_orders("pending")

    assert removed_count == 1
    assert runner.state.open_orders == {}
    assert exchange.placed_orders == []
    assert runner._pending_trade_events[0]["event_type"] == "order_cancelled"
    assert runner._pending_trade_events[0]["level_index"] == 0


def test_restored_live_runner_takes_over_existing_exchange_orders(monkeypatch):
    class _RecoveredLiveExchange(_StubExchange):
        def __init__(self) -> None:
            super().__init__([100.0])
            self.synced_levels: set[int] = set()

        def has_tracked_level_order(self, level_index: int, side: str, limit_price: float) -> bool:  # noqa: ARG002
            return level_index in self.synced_levels

    exchange = _RecoveredLiveExchange()
    config = _config().model_copy(update={"mode": "live"})
    sync_calls = {"count": 0}

    def _fake_sync(self):
        sync_calls["count"] += 1
        exchange.synced_levels = set(self.state.open_orders.keys())
        return set(exchange.synced_levels)

    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, cfg: exchange)
    monkeypatch.setattr(BotRunner, "_sync_existing_live_open_orders", _fake_sync)
    monkeypatch.setattr(BotRunner, "_loop", lambda self: None)
    monkeypatch.setattr("agent.app.runner.time.sleep", lambda _: None)

    runner = BotRunner("bot-live-restore", config, "http://manager:8000", "agent-1", AgentLogStore())
    runner.restore_runner_state(
        runner.get_runner_state().model_copy(
            update={
                "open_orders": {0: "buy", 1: "buy"},
                "price": 100.0,
                "quote_balance": 1000.0,
                "base_balance": 0.0,
            }
        )
    )
    runner.running = True

    runner._startup_and_loop(restored=True)

    assert sync_calls["count"] == 1
    assert exchange.synced_levels == {0, 1}
    assert exchange.placed_orders == []


def test_live_snapshot_includes_reserved_open_order_value(monkeypatch):
    class _LiveReservedExchange(_StubExchange):
        def __init__(self) -> None:
            super().__init__([100.0])
            self._limit_orders = {
                "buy-1": {
                    "side": "buy",
                    "quote_amount": 400.0,
                    "limit_price": 95.0,
                }
            }
            self.quote_balance = 600.0
            self.base_balance = 0.0

    exchange = _LiveReservedExchange()
    config = _config().model_copy(update={"mode": "live", "budget": BudgetConfig(quote_budget=57.0, base_budget=0.0, profit_mode="compound", skim_ratio=0.5)})
    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, cfg: exchange)

    runner = BotRunner("bot-live-equity", config, "http://manager:8000", "agent-1", AgentLogStore())
    runner.price = 100.0

    snapshot = runner._build_snapshot("running")

    assert snapshot.total_equity_quote == pytest.approx(57.0)
    assert snapshot.unrealized_pnl_quote == pytest.approx(0.0)


def test_push_snapshot_clears_pending_trade_events_on_success(monkeypatch):
    class _OkResponse:
        def raise_for_status(self) -> None:
            return None

    exchange = _StubExchange([100.0])
    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)
    monkeypatch.setattr("agent.app.runner.requests.post", lambda *args, **kwargs: _OkResponse())

    runner = BotRunner("bot-push-ok", _config(), "http://manager:8000", "agent-1", AgentLogStore())
    runner.price = 100.0
    runner._pending_trade_events = [
        {
            "event_type": "order_filled",
            "order_id": "ord-1",
            "side": "buy",
            "quote_amount": 10.0,
            "price": 100.0,
            "level_index": 1,
            "trade_pnl": 0.0,
            "total_equity": 1000.0,
            "trade_number": 1,
        }
    ]

    runner._push_snapshot(runner._build_snapshot("running"))

    assert runner._pending_trade_events == []


def test_push_snapshot_requeues_trade_events_on_http_error(monkeypatch):
    class _FailResponse:
        def raise_for_status(self) -> None:
            raise requests.HTTPError("HTTP 500")

    exchange = _StubExchange([100.0])
    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)
    monkeypatch.setattr("agent.app.runner.requests.post", lambda *args, **kwargs: _FailResponse())

    runner = BotRunner("bot-push-fail", _config(), "http://manager:8000", "agent-1", AgentLogStore())
    runner.price = 100.0
    event = {
        "event_type": "order_filled",
        "order_id": "ord-2",
        "side": "sell",
        "quote_amount": 11.0,
        "price": 101.0,
        "level_index": 2,
        "trade_pnl": 0.1,
        "total_equity": 1000.1,
        "trade_number": 2,
    }
    runner._pending_trade_events = [event]

    runner._push_snapshot(runner._build_snapshot("running"))

    assert runner._pending_trade_events == [event]


def test_sync_with_exchange_uses_bot_authoritative_state_and_recovered_fills(monkeypatch):
    class _FakeLiveExchange:
        def __init__(self):
            self.quote_balance = 500.0
            self.base_balance = 2.5

        def ensure_authenticated(self) -> None:
            return None

        def force_authoritative_grid_sync(self, level_prices, quote_amount):  # noqa: ARG002
            return {
                "open_orders": {0: "buy", 4: "sell"},
                "fills": [
                    {
                        "order_id": "hist-1",
                        "side": "sell",
                        "quote_amount": 9.5,
                        "fill_price": 101.0,
                        "base_amount": 0.094059,
                        "level_index": 4,
                        "fill_count": 1,
                        "fee_paid_quote": 0.01,
                        "fee_rate": 0.001,
                    }
                ],
                "synced_levels": [0, 4],
            }

        def get_balances(self):
            return self.quote_balance, self.base_balance

        def has_tracked_level_order(self, level_index: int, side: str, limit_price: float) -> bool:  # noqa: ARG002
            return True

    config = _config().model_copy(update={"mode": "live"})
    exchange = _FakeLiveExchange()

    monkeypatch.setattr("agent.app.runner.BitvavoExchange", _FakeLiveExchange)
    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, cfg: exchange)
    monkeypatch.setattr(BotRunner, "_push_snapshot", lambda self, snapshot: None)

    runner = BotRunner("bot-authoritative-sync", config, "http://manager:8000", "agent-1", AgentLogStore())
    runner.running = True
    runner.state.open_orders = {1: "buy"}
    runner.trade_count = 7

    details = runner.sync_with_exchange()

    assert runner.state.open_orders == {0: "buy", 4: "sell"}
    assert details["synced_levels"] == [0, 4]
    assert details["recovered_fills"] == 1
    assert runner.trade_count == 8
    assert len(runner._pending_trade_events) == 1
    assert runner._pending_trade_events[0]["event_type"] == "order_filled"
    assert runner._pending_trade_events[0]["order_id"] == "hist-1"


def test_runner_manager_prepare_delete_removes_runner_on_success():
    class _DeleteOkRunner:
        def prepare_delete(self, mode):
            return {"mode": mode, "ok": True}

    manager = RunnerManager("http://manager:8000", "agent-1")
    manager.runners["bot-delete"] = _DeleteOkRunner()  # type: ignore[assignment]

    details = manager.prepare_delete("bot-delete", "delete_open_orders")

    assert details["ok"] is True
    assert "bot-delete" not in manager.runners


def test_runner_manager_prepare_delete_keeps_runner_on_failure():
    class _DeleteFailRunner:
        def prepare_delete(self, mode):
            raise RuntimeError(f"failed mode={mode}")

    manager = RunnerManager("http://manager:8000", "agent-1")
    manager.runners["bot-delete-fail"] = _DeleteFailRunner()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="failed mode=delete_open_orders"):
        manager.prepare_delete("bot-delete-fail", "delete_open_orders")

    assert "bot-delete-fail" in manager.runners


def test_prepare_delete_open_orders_live_cancels_buy_and_sell_scoped(monkeypatch):
    class _DeleteLiveExchange(_StubExchange):
        def __init__(self):
            super().__init__([100.0])
            self.quote_balance = 500.0
            self.base_balance = 2.0
            self.calls: list[str] = []

        def cancel_operator_orders(self, side=None):
            self.calls.append(str(side))
            if side == "buy":
                return {"cancelled": 3, "cancelled_sell_base_amount": 0.0}
            if side == "sell":
                return {"cancelled": 2, "cancelled_sell_base_amount": 1.25}
            return {"cancelled": 0, "cancelled_sell_base_amount": 0.0}

    exchange = _DeleteLiveExchange()
    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)

    cfg = _config().model_copy(update={"mode": "live"})
    runner = BotRunner("bot-delete-live", cfg, "http://manager:8000", "agent-1", AgentLogStore())

    details = runner.prepare_delete("delete_open_orders")

    assert details["mode"] == "delete_open_orders"
    assert details["cancelled_buy_orders"] == 3
    assert details["cancelled_sell_orders"] == 2
    assert exchange.calls == ["buy", "sell"]


def test_prepare_delete_transform_to_quote_adds_cancelled_sell_base(monkeypatch):
    class _DeleteQuoteExchange(_StubExchange):
        def __init__(self):
            super().__init__([100.0])
            self.quote_balance = 500.0
            self.base_balance = 1.0
            self.executed: list[dict] = []

        def cancel_operator_orders(self, side=None):
            if side == "buy":
                return {"cancelled": 1, "cancelled_sell_base_amount": 0.0}
            if side == "sell":
                return {"cancelled": 2, "cancelled_sell_base_amount": 0.5}
            return {"cancelled": 0, "cancelled_sell_base_amount": 0.0}

        def execute(self, signal, price=None):
            self.executed.append({"side": signal.side, "quote_amount": signal.quote_amount, "price": price})
            return True

    exchange = _DeleteQuoteExchange()
    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)

    cfg = _config().model_copy(update={"mode": "live"})
    runner = BotRunner("bot-delete-quote", cfg, "http://manager:8000", "agent-1", AgentLogStore())
    runner.price = 100.0

    details = runner.prepare_delete("transform_to_quote")

    assert details["mode"] == "transform_to_quote"
    assert details["cancelled_buy_orders"] == 1
    assert details["cancelled_sell_orders"] == 2
    assert details["cancelled_sell_base_amount"] == pytest.approx(0.5)
    assert len(exchange.executed) == 1
    assert exchange.executed[0]["side"] == "sell"
    # (base_balance + cancelled_sell_base) * price = (1.0 + 0.5) * 100
    assert exchange.executed[0]["quote_amount"] == pytest.approx(150.0)