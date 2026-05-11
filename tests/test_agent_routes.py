from __future__ import annotations

import time
from unittest.mock import MagicMock

from agent.app import routes


def _reset_agent_balance_cache() -> None:
    with routes._BALANCE_CACHE_LOCK:
        routes._BALANCE_ROWS_CACHE["ts"] = 0.0
        routes._BALANCE_ROWS_CACHE["rows"] = []


def test_notifications_balance_uses_temporary_exchange_when_no_live_exchange(monkeypatch):
    _reset_agent_balance_cache()
    fake_exchange = MagicMock()

    monkeypatch.setattr(routes, "_pick_exchange_for_market", lambda market: None)
    monkeypatch.setattr(routes, "_create_temporary_bitvavo_exchange", lambda market: fake_exchange)
    monkeypatch.setattr(
        routes,
        "_call_action_list",
        lambda exchange, action, payload=None, timeout=8.0: [
            {"symbol": "EUR", "available": "10", "inOrder": "2"},
            {"symbol": "BTC", "available": "0.1", "inOrder": "0.0"},
        ],
    )
    monkeypatch.setattr(routes, "_ticker_24h_for_market", lambda market: {"last_price": 50000.0, "change_pct": 1.5})

    result = routes.notifications_balance()

    assert "rows" in result
    assert len(result["rows"]) == 2
    assert any(row["asset"] == "EUR" for row in result["rows"])
    assert any(row["asset"] == "BTC" for row in result["rows"])
    fake_exchange.stop.assert_called_once()


def test_notifications_balance_reuses_fresh_cache_without_exchange_call(monkeypatch):
    now = time.time()
    with routes._BALANCE_CACHE_LOCK:
        routes._BALANCE_ROWS_CACHE["ts"] = now
        routes._BALANCE_ROWS_CACHE["rows"] = [
            {
                "asset": "EUR",
                "price": 1.0,
                "change_24h": 0.0,
                "euro_value": 50.0,
                "balance": 50.0,
                "available_balance": 50.0,
                "in_orders": 0.0,
            }
        ]

    called = {"acquire": 0}

    def _acquire(_market):
        called["acquire"] += 1
        return None, False

    monkeypatch.setattr(routes, "_acquire_exchange_for_market", _acquire)

    result = routes.notifications_balance()

    assert called["acquire"] == 0
    assert result["rows"]
    assert result["rows"][0]["asset"] == "EUR"


def test_notifications_balance_uses_stale_cache_when_exchange_unavailable(monkeypatch):
    _reset_agent_balance_cache()
    with routes._BALANCE_CACHE_LOCK:
        routes._BALANCE_ROWS_CACHE["ts"] = 0.0
        routes._BALANCE_ROWS_CACHE["rows"] = [
            {
                "asset": "BTC",
                "price": 50000.0,
                "change_24h": 1.2,
                "euro_value": 1000.0,
                "balance": 0.02,
                "available_balance": 0.02,
                "in_orders": 0.0,
            }
        ]

    monkeypatch.setattr(routes, "_acquire_exchange_for_market", lambda _market: (None, False))

    result = routes.notifications_balance()

    assert len(result["rows"]) == 1
    assert result["rows"][0]["asset"] == "BTC"
