"""Unit tests for manager.app.services – backtest, grid_preview, agent_client."""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest

from manager.app.services.agent_client import post_json
from manager.app.services.backtest import run_backtest
from manager.app.services.grid_preview import build_static_grid_profit_preview
from common import BotConfig, GridConfig, BudgetConfig


# ── Helpers ──────────────────────────────────────────────────────────

def _bot_config(**overrides) -> BotConfig:
    defaults = {
        "market": "BTC-EUR",
        "base_currency": "BTC",
        "quote_currency": "EUR",
        "mode": "simulation",
        "strategy": "static_grid",
        "start_price": 100.0,
        "grid": GridConfig(lower_price=90.0, upper_price=110.0, levels=5, order_size_quote=10.0),
        "budget": BudgetConfig(quote_budget=100.0, base_budget=0.0),
    }
    defaults.update(overrides)
    return BotConfig(**defaults)


# ── agent_client.post_json ───────────────────────────────────────────


class TestPostJson:
    @patch("manager.app.services.agent_client.requests.post")
    def test_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = '{"ok": true}'
        mock_post.return_value = mock_resp

        ok, msg = post_json("http://agent:8100/start", {"bot_id": "1"})
        assert ok is True
        assert msg == '{"ok": true}'

    @patch("manager.app.services.agent_client.requests.post")
    def test_http_error(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Error"
        mock_post.return_value = mock_resp

        ok, msg = post_json("http://agent:8100/start", {})
        assert ok is False
        assert "500" in msg

    @patch("manager.app.services.agent_client.requests.post")
    def test_network_error(self, mock_post):
        import requests as req
        mock_post.side_effect = req.ConnectionError("refused")

        ok, msg = post_json("http://agent:8100/start", {})
        assert ok is False
        assert "refused" in msg

    @patch("manager.app.services.agent_client.requests.post")
    def test_timeout(self, mock_post):
        import requests as req
        mock_post.side_effect = req.Timeout("timed out")

        ok, msg = post_json("http://agent:8100/start", {}, timeout=1.0)
        assert ok is False
        assert "timed out" in msg


# ── backtest.run_backtest ────────────────────────────────────────────


class TestRunBacktest:
    def test_with_explicit_prices(self):
        cfg = _bot_config()
        prices = [100.0, 95.0, 90.0, 95.0, 100.0, 105.0, 110.0]
        result = run_backtest(cfg, prices)
        assert "initial_equity_quote" in result
        assert "final_equity_quote" in result
        assert "trades_executed" in result
        assert isinstance(result["trades_executed"], int)

    def test_auto_generated_prices(self):
        cfg = _bot_config()
        result = run_backtest(cfg, None)
        assert result["trades_executed"] >= 0

    def test_single_price(self):
        cfg = _bot_config()
        result = run_backtest(cfg, [100.0])
        assert result["initial_equity_quote"] == result["final_equity_quote"]
        assert result["total_pnl_quote"] == pytest.approx(0.0)

    def test_flat_prices_no_trades(self):
        # Price stays exactly at start – may or may not trigger grid depending on levels
        cfg = _bot_config()
        result = run_backtest(cfg, [100.0] * 20)
        assert result["trades_executed"] >= 0

    def test_extreme_price_swing(self):
        cfg = _bot_config(start_price=100.0)
        prices = [100.0, 50.0, 200.0, 50.0, 200.0]
        result = run_backtest(cfg, prices)
        assert isinstance(result["final_equity_quote"], float)


# ── grid_preview.build_static_grid_profit_preview ────────────────────


class TestGridPreview:
    def test_wide_grid_is_profitable(self):
        grid = GridConfig(lower_price=90.0, upper_price=110.0, levels=5, order_size_quote=100.0)
        result = build_static_grid_profit_preview(grid, fee_rate=0.001)
        assert result["is_profitable"] is True
        assert result["total_trade_paths"] == 4
        assert result["step_size"] == pytest.approx(5.0)
        assert result["fee_rate"] == pytest.approx(0.001)

    def test_narrow_grid_high_fees_unprofitable(self):
        grid = GridConfig(lower_price=99.0, upper_price=101.0, levels=2, order_size_quote=100.0)
        result = build_static_grid_profit_preview(grid, fee_rate=0.05)
        assert result["is_profitable"] is False

    def test_zero_fee(self):
        grid = GridConfig(lower_price=90.0, upper_price=110.0, levels=3, order_size_quote=50.0)
        result = build_static_grid_profit_preview(grid, fee_rate=0.0)
        assert result["is_profitable"] is True
        # With zero fees every upward step is profitable
        assert result["profitable_trades"] == result["total_trade_paths"]

    def test_many_levels(self):
        grid = GridConfig(lower_price=100.0, upper_price=200.0, levels=50, order_size_quote=10.0)
        result = build_static_grid_profit_preview(grid, fee_rate=0.0025)
        assert result["total_trade_paths"] == 49
        assert result["step_size"] == pytest.approx((200 - 100) / 49)

    def test_two_levels_minimum(self):
        grid = GridConfig(lower_price=100.0, upper_price=110.0, levels=2, order_size_quote=10.0)
        result = build_static_grid_profit_preview(grid, fee_rate=0.001)
        assert result["total_trade_paths"] == 1

    def test_step_percent_calculation(self):
        grid = GridConfig(lower_price=100.0, upper_price=200.0, levels=3, order_size_quote=10.0)
        result = build_static_grid_profit_preview(grid, fee_rate=0.0)
        # step = 50, step_percent = 50/100 * 100 = 50%
        assert result["step_percent"] == pytest.approx(50.0)

    def test_profit_ordering(self):
        grid = GridConfig(lower_price=100.0, upper_price=200.0, levels=10, order_size_quote=10.0)
        result = build_static_grid_profit_preview(grid, fee_rate=0.001)
        assert result["profit_per_trade_quote_min"] <= result["profit_per_trade_quote_avg"]
        assert result["profit_per_trade_quote_avg"] <= result["profit_per_trade_quote_max"]
