from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from common import BotConfig, BudgetConfig, GridConfig
from agent.app.runner import BotRunner, AgentLogStore


class _FakeExchange:
    def __init__(self) -> None:
        self.quote_balance = 100.0
        self.base_balance = 2.0
        self.price = 50.0

    def start(self) -> None:
        return None

    def get_balances(self) -> tuple[float, float]:
        return self.quote_balance, self.base_balance


def _config() -> BotConfig:
    return BotConfig(
        market="BTC-EUR",
        base_currency="BTC",
        quote_currency="EUR",
        mode="simulation",
        strategy="static_grid",
        fee_rate=0.0,
        grid=GridConfig(lower_price=40.0, upper_price=60.0, levels=3, order_size_quote=10.0),
        budget=BudgetConfig(quote_budget=100.0, base_budget=2.0),
    )


def test_bot_runner_snapshot_uses_start_time_for_runtime() -> None:
    fake_exchange = _FakeExchange()
    with patch.object(BotRunner, "_build_exchange", return_value=fake_exchange):
        runner = BotRunner("bot-1", _config(), "http://manager", "agent-1", AgentLogStore())

    runner.started_at = datetime.now(timezone.utc) - timedelta(seconds=125)

    snapshot = runner._build_snapshot("running")

    assert 124 <= snapshot.runtime_seconds <= 126
    assert snapshot.status == "running"
