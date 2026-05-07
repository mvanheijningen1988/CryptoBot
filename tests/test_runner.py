from __future__ import annotations

from common import BotConfig, BudgetConfig, GridConfig
from agent.app.runner import AgentLogStore, BotRunner


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
    ) -> bool:
        self.placed_orders.append({
            "order_id": order_id,
            "side": side,
            "quote_amount": quote_amount,
            "limit_price": limit_price,
            "level_index": level_index,
        })
        return True

    def get_filled_orders(self) -> list[dict]:
        return []


def _config() -> BotConfig:
    return BotConfig(
        market="BTC-EUR",
        base_currency="BTC",
        quote_currency="EUR",
        mode="simulation",
        strategy="static_grid",
        grid=GridConfig(lower_price=90.0, upper_price=110.0, levels=5, order_size_quote=100.0),
        budget=BudgetConfig(quote_budget=1000.0, base_budget=0.0),
    )


def test_runner_waits_for_real_price_before_initial_orders(monkeypatch):
    exchange = _StubExchange([0.0, 100.0])
    snapshots = []

    monkeypatch.setattr(BotRunner, "_build_exchange", lambda self, config: exchange)
    monkeypatch.setattr(BotRunner, "_loop", lambda self: None)
    monkeypatch.setattr(BotRunner, "_push_snapshot", lambda self, snapshot: snapshots.append(snapshot))
    monkeypatch.setattr("agent.app.runner.time.sleep", lambda _: None)

    runner = BotRunner("bot-1", _config(), "http://manager:8000", "agent-1", AgentLogStore())
    runner.running = True

    runner._startup_and_loop(restored=False)

    assert [order["level_index"] for order in exchange.placed_orders] == [0, 1]
    assert all(order["side"] == "buy" for order in exchange.placed_orders)
    assert snapshots[-1].status == "running"
    assert runner.price == 100.0