"""Integration tests for the manager FastAPI endpoints.

Uses the ``client`` fixture from conftest which wires a TestClient to an
in-memory SQLite database.  Every test starts with a clean DB.
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from manager.app.auth import create_token, hash_password
from manager.app.models import Agent, Bot, User


# ── Helpers ──────────────────────────────────────────────────────────


def _get_db(client):
    """Get a DB session from the test client's dependency override."""
    from manager.app.database import get_db

    db_gen = client.app.dependency_overrides[get_db]()
    return next(db_gen)


def _seed_admin(client):
    """Return the admin user created by startup, or create one, with token and header."""
    db = _get_db(client)

    user = db.query(User).filter(User.username == "admin").first()
    if not user:
        user = User(
            id="admin-ep-id",
            username="admin",
            password_hash=hash_password("changeme123"),
            role="admin",
            locale="en",
            must_change_password=False,
        )
        db.add(user)
        db.commit()
    else:
        # Ensure known password and flags for tests
        user.password_hash = hash_password("changeme123")
        user.must_change_password = False
        db.commit()

    token = create_token(user.id, user.role)
    header = {"Authorization": f"Bearer {token}"}
    return user, token, header


def _seed_viewer(client):
    """Create a viewer user and return (user, token, header)."""
    db = _get_db(client)

    user = db.query(User).filter(User.username == "viewer").first()
    if not user:
        user = User(
            id="viewer-ep-id",
            username="viewer",
            password_hash=hash_password("viewerpass1"),
            role="viewer",
            locale="en",
            must_change_password=False,
        )
        db.add(user)
        db.commit()

    token = create_token(user.id, user.role)
    header = {"Authorization": f"Bearer {token}"}
    return user, token, header


def _seed_agent(client, agent_id="agent-1", approval="approved", status="online"):
    """Insert an agent directly and return it."""
    from datetime import UTC, datetime

    db = _get_db(client)

    agent = Agent(
        id=agent_id,
        base_url="http://agent:8100",
        status=status,
        approval_status=approval,
        capacity=5,
        last_heartbeat=datetime.now(UTC),
    )
    db.add(agent)
    db.commit()
    return agent


def _seed_bot(client, bot_id="bot-1", agent_id=None, status="stopped"):
    """Insert a bot directly and return it."""
    from datetime import UTC, datetime

    db = _get_db(client)

    bot = Bot(
        id=bot_id,
        name=f"Bot {bot_id}",
        strategy_type="static_grid",
        mode="simulation",
        status=status,
        assigned_agent_id=agent_id,
        config_json=json.dumps({
            "market": "BTC-EUR",
            "base_currency": "BTC",
            "quote_currency": "EUR",
            "mode": "simulation",
            "strategy": "static_grid",
            "start_price": 50000.0,
            "grid": {
                "lower_price": 48000.0,
                "upper_price": 52000.0,
                "levels": 5,
                "order_size_quote": 100.0,
            },
            "budget": {
                "quote_budget": 1000.0,
                "base_budget": 0.0,
                "profit_mode": "compound",
                "skim_ratio": 0.5,
            },
        }),
        latest_metrics_json="{}",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(bot)
    db.commit()
    return bot


# ── Health ───────────────────────────────────────────────────────────


class TestHealth:
    def test_health_ok(self, client):
        r = client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "ok"


# ── Static pages ─────────────────────────────────────────────────────


class TestStaticPages:
    def test_root_serves_index(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]

    def test_login_page(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert "text/html" in r.headers["content-type"]


# ── Auth endpoints ───────────────────────────────────────────────────


class TestAuthLogin:
    def test_valid_login(self, client):
        _seed_admin(client)
        r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "changeme123"})  # NOSONAR
        assert r.status_code == 200
        body = r.json()
        assert "token" in body
        assert body["user"]["username"] == "admin"
        assert body["user"]["role"] == "admin"

    def test_wrong_password(self, client):
        _seed_admin(client)
        r = client.post("/api/v1/auth/login", json={"username": "admin", "password": "wrong"})  # NOSONAR
        assert r.status_code == 401

    def test_nonexistent_user(self, client):
        r = client.post("/api/v1/auth/login", json={"username": "ghost", "password": "x"})  # NOSONAR
        assert r.status_code == 401

    def test_empty_credentials(self, client):
        r = client.post("/api/v1/auth/login", json={"username": "", "password": ""})
        assert r.status_code == 401

    def test_missing_fields(self, client):
        r = client.post("/api/v1/auth/login", json={})
        assert r.status_code == 401


class TestAuthMe:
    def test_returns_current_user(self, client):
        _, _, header = _seed_admin(client)
        r = client.get("/api/v1/auth/me", headers=header)
        assert r.status_code == 200
        assert r.json()["username"] == "admin"

    def test_no_token_returns_401(self, client):
        r = client.get("/api/v1/auth/me")
        assert r.status_code == 401

    def test_invalid_token_returns_401(self, client):
        r = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer garbage"})
        assert r.status_code == 401


class TestChangePassword:
    def test_change_password(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/auth/change-password",
            json={"new_password": "newpass123"},  # NOSONAR
            headers=header,
        )
        assert r.status_code == 200
        # Verify new password works
        r2 = client.post("/api/v1/auth/login", json={"username": "admin", "password": "newpass123"})  # NOSONAR
        assert r2.status_code == 200

    def test_short_password_rejected(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/auth/change-password",
            json={"new_password": "ab"},  # NOSONAR
            headers=header,
        )
        assert r.status_code == 400

    def test_empty_password_rejected(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/auth/change-password",
            json={"new_password": ""},
            headers=header,
        )
        assert r.status_code == 400

    def test_unauthenticated_rejected(self, client):
        r = client.post("/api/v1/auth/change-password", json={"new_password": "newpass123"})  # NOSONAR
        assert r.status_code == 401


class TestUpdateLocale:
    def test_set_nl(self, client):
        _, _, header = _seed_admin(client)
        r = client.post("/api/v1/auth/locale", json={"locale": "nl"}, headers=header)
        assert r.status_code == 200
        assert r.json()["locale"] == "nl"

    def test_unsupported_locale(self, client):
        _, _, header = _seed_admin(client)
        r = client.post("/api/v1/auth/locale", json={"locale": "fr"}, headers=header)
        assert r.status_code == 400

    def test_unauthenticated(self, client):
        r = client.post("/api/v1/auth/locale", json={"locale": "en"})
        assert r.status_code == 401


# ── User management ──────────────────────────────────────────────────


class TestListUsers:
    def test_admin_can_list(self, client):
        _, _, header = _seed_admin(client)
        r = client.get("/api/v1/users", headers=header)
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_viewer_forbidden(self, client):
        _, _, header = _seed_viewer(client)
        r = client.get("/api/v1/users", headers=header)
        assert r.status_code == 403


class TestCreateUser:
    def test_admin_creates_user(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/users",
            json={"username": "newguy", "password": "password1", "role": "viewer"},
            headers=header,
        )
        assert r.status_code == 200
        assert r.json()["username"] == "newguy"
        assert r.json()["role"] == "viewer"

    def test_duplicate_username(self, client):
        _, _, header = _seed_admin(client)
        client.post(
            "/api/v1/users",
            json={"username": "dup", "password": "password1", "role": "viewer"},
            headers=header,
        )
        r = client.post(
            "/api/v1/users",
            json={"username": "dup", "password": "password2", "role": "viewer"},
            headers=header,
        )
        assert r.status_code == 409

    def test_invalid_role(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/users",
            json={"username": "x", "password": "password1", "role": "superadmin"},
            headers=header,
        )
        assert r.status_code == 400

    def test_short_password(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/users",
            json={"username": "x", "password": "ab", "role": "viewer"},  # NOSONAR
            headers=header,
        )
        assert r.status_code == 400

    def test_empty_username(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/users",
            json={"username": "", "password": "password1", "role": "viewer"},
            headers=header,
        )
        assert r.status_code == 400

    def test_viewer_cannot_create(self, client):
        _, _, header = _seed_viewer(client)
        r = client.post(
            "/api/v1/users",
            json={"username": "x", "password": "password1", "role": "viewer"},
            headers=header,
        )
        assert r.status_code == 403


class TestDeleteUser:
    def test_admin_deletes_user(self, client):
        _, _, header = _seed_admin(client)
        # Create a user to delete
        r = client.post(
            "/api/v1/users",
            json={"username": "todelete", "password": "password1", "role": "viewer"},
            headers=header,
        )
        uid = r.json()["id"]
        r2 = client.delete(f"/api/v1/users/{uid}", headers=header)
        assert r2.status_code == 200

    def test_cannot_delete_self(self, client):
        user, _, header = _seed_admin(client)
        r = client.delete(f"/api/v1/users/{user.id}", headers=header)
        assert r.status_code == 400

    def test_delete_nonexistent(self, client):
        _, _, header = _seed_admin(client)
        r = client.delete("/api/v1/users/does-not-exist", headers=header)
        assert r.status_code == 404

    def test_viewer_cannot_delete(self, client):
        _, _, header = _seed_viewer(client)
        r = client.delete("/api/v1/users/some-id", headers=header)
        assert r.status_code == 403


# ── Agent endpoints ──────────────────────────────────────────────────


class TestRegisterAgent:
    def test_register_new_agent(self, client):
        r = client.post("/api/v1/agents/register", json={
            "agent_id": "new-agent",
            "base_url": "http://new:8100",
            "capacity": 3,
        })
        assert r.status_code == 200
        assert r.json()["approval_status"] == "pending"

    def test_register_existing_agent_updates(self, client):
        client.post("/api/v1/agents/register", json={
            "agent_id": "same-agent",
            "base_url": "http://original:8100",
            "capacity": 5,
        })
        r = client.post("/api/v1/agents/register", json={
            "agent_id": "same-agent",
            "base_url": "http://updated:8100",
            "capacity": 10,
        })
        assert r.status_code == 200


class TestHeartbeat:
    def test_heartbeat_approved_agent(self, client):
        _seed_agent(client, "hb-agent")
        r = client.post("/api/v1/agents/hb-agent/heartbeat", json={"status": "online"})
        assert r.status_code == 200

    def test_heartbeat_nonexistent(self, client):
        r = client.post("/api/v1/agents/ghost/heartbeat", json={"status": "online"})
        assert r.status_code == 404


class TestApproveAgent:
    def test_approve_pending(self, client):
        _seed_agent(client, "ap-agent", approval="pending", status="pending")
        r = client.post("/api/v1/agents/ap-agent/approve")
        assert r.status_code == 200

    def test_approve_nonexistent(self, client):
        r = client.post("/api/v1/agents/ghost/approve")
        assert r.status_code == 404


class TestRejectAgent:
    def test_reject_agent(self, client):
        _seed_agent(client, "rj-agent", approval="pending", status="pending")
        r = client.post("/api/v1/agents/rj-agent/reject")
        assert r.status_code == 200

    def test_reject_nonexistent(self, client):
        r = client.post("/api/v1/agents/ghost/reject")
        assert r.status_code == 404


class TestUnapproveAgent:
    def test_unapprove_agent(self, client):
        _seed_agent(client, "un-agent", approval="approved", status="online")
        r = client.post("/api/v1/agents/un-agent/unapprove")
        assert r.status_code == 200

    def test_unapprove_nonexistent(self, client):
        r = client.post("/api/v1/agents/ghost/unapprove")
        assert r.status_code == 404


class TestListAgents:
    def test_empty(self, client):
        r = client.get("/api/v1/agents")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_agents(self, client):
        _seed_agent(client, "list-a")
        r = client.get("/api/v1/agents")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["id"] == "list-a"


class TestAgentEvents:
    def test_returns_list(self, client):
        r = client.get("/api/v1/agent-events")
        assert r.status_code == 200
        assert isinstance(r.json(), list)


class TestAgentLogs:
    def test_unapproved_agent_returns_400(self, client):
        _seed_agent(client, "logs-agent", approval="pending", status="pending")
        r = client.get("/api/v1/agents/logs-agent/logs")
        assert r.status_code == 400

    def test_nonexistent_agent_returns_404(self, client):
        r = client.get("/api/v1/agents/ghost/logs")
        assert r.status_code == 404


# ── Bot endpoints ────────────────────────────────────────────────────


class TestCreateBot:
    def test_create_bot(self, client):
        r = client.post("/api/v1/bots", json={
            "name": "My Bot",
            "config": {
                "market": "BTC-EUR",
                "base_currency": "BTC",
                "quote_currency": "EUR",
                "mode": "simulation",
                "strategy": "static_grid",
                "start_price": 50000.0,
                "grid": {
                    "lower_price": 48000.0,
                    "upper_price": 52000.0,
                    "levels": 5,
                    "order_size_quote": 100.0,
                },
                "budget": {
                    "quote_budget": 1000.0,
                    "base_budget": 0.0,
                    "profit_mode": "compound",
                    "skim_ratio": 0.5,
                },
            },
        })
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "My Bot"
        assert body["status"] == "stopped"
        assert body["assigned_agent_id"] is None


class TestListBots:
    def test_empty(self, client):
        r = client.get("/api/v1/bots")
        assert r.status_code == 200
        assert r.json() == []

    def test_returns_bots(self, client):
        _seed_agent(client, "lb-agent")
        _seed_bot(client, "lb-bot", agent_id="lb-agent")
        r = client.get("/api/v1/bots")
        assert r.status_code == 200
        assert len(r.json()) == 1


class TestStartBot:
    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    def test_start_with_agent(self, mock_post, client):
        _seed_agent(client, "start-agent")
        _seed_bot(client, "start-bot", agent_id="start-agent")
        r = client.post("/api/v1/bots/start-bot/start", json={"agent_id": "start-agent"})
        assert r.status_code == 200

    def test_start_nonexistent_bot(self, client):
        r = client.post("/api/v1/bots/ghost/start", json={})
        assert r.status_code == 404

    def test_start_no_agent_available(self, client):
        _seed_bot(client, "lonely-bot")
        r = client.post("/api/v1/bots/lonely-bot/start", json={})
        assert r.status_code == 400

    @patch("manager.app.routes.bots.post_json", return_value=(False, "connection refused"))
    def test_start_agent_failure(self, mock_post, client):
        _seed_agent(client, "fail-agent")
        _seed_bot(client, "fail-bot", agent_id="fail-agent")
        r = client.post("/api/v1/bots/fail-bot/start", json={"agent_id": "fail-agent"})
        assert r.status_code == 502


class TestStopBot:
    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    def test_stop_running_bot(self, mock_post, client):
        _seed_agent(client, "stop-agent")
        _seed_bot(client, "stop-bot", agent_id="stop-agent", status="running")
        r = client.post("/api/v1/bots/stop-bot/stop")
        assert r.status_code == 200

    def test_stop_nonexistent_bot(self, client):
        r = client.post("/api/v1/bots/ghost/stop")
        assert r.status_code == 404

    def test_stop_unassigned_bot(self, client):
        _seed_bot(client, "unassigned-bot")
        r = client.post("/api/v1/bots/unassigned-bot/stop")
        assert r.status_code == 200


class TestUpdateBudget:
    def test_update_budget_stopped_bot(self, client):
        _seed_agent(client, "bud-agent")
        _seed_bot(client, "bud-bot", agent_id="bud-agent")
        r = client.post(
            "/api/v1/bots/bud-bot/budget",
            json={"quote_budget": 2000.0, "base_budget": 0.5},
        )
        assert r.status_code == 200

    def test_update_budget_nonexistent(self, client):
        r = client.post(
            "/api/v1/bots/ghost/budget",
            json={"quote_budget": 100.0, "base_budget": 0.0},
        )
        assert r.status_code == 404

    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    def test_update_budget_running_bot_forwards_to_agent(self, mock_post, client):
        _seed_agent(client, "brun-agent")
        _seed_bot(client, "brun-bot", agent_id="brun-agent", status="running")
        r = client.post(
            "/api/v1/bots/brun-bot/budget",
            json={"quote_budget": 3000.0, "base_budget": 1.0},
        )
        assert r.status_code == 200
        mock_post.assert_called_once()


class TestPushMetrics:
    def test_push_metrics(self, client):
        _seed_agent(client, "met-agent")
        _seed_bot(client, "met-bot", agent_id="met-agent")
        r = client.post(
            "/api/v1/agents/met-agent/bots/met-bot/metrics",
            json={
                "snapshot": {
                    "bot_id": "met-bot",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "price": 50000.0,
                    "quote_balance": 900.0,
                    "base_balance": 0.002,
                    "base_value_in_quote": 100.0,
                    "total_equity_quote": 1000.0,
                    "realized_pnl_quote": 10.0,
                    "unrealized_pnl_quote": 5.0,
                    "skimmed_quote": 0.0,
                    "status": "running",
                },
            },
        )
        assert r.status_code == 200

    def test_push_metrics_nonexistent_bot(self, client):
        _seed_agent(client, "met2-agent")
        r = client.post(
            "/api/v1/agents/met2-agent/bots/ghost/metrics",
            json={
                "snapshot": {
                    "bot_id": "ghost",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "price": 100.0,
                    "quote_balance": 0,
                    "base_balance": 0,
                    "base_value_in_quote": 0,
                    "total_equity_quote": 0,
                    "realized_pnl_quote": 0,
                    "unrealized_pnl_quote": 0,
                    "skimmed_quote": 0,
                    "status": "stopped",
                },
            },
        )
        assert r.status_code == 404


# ── Backtest ─────────────────────────────────────────────────────────


class TestBacktest:
    def test_backtest_with_prices(self, client):
        r = client.post("/api/v1/backtest", json={
            "config": {
                "market": "BTC-EUR",
                "base_currency": "BTC",
                "quote_currency": "EUR",
                "mode": "simulation",
                "strategy": "static_grid",
                "start_price": 100.0,
                "grid": {
                    "lower_price": 90.0,
                    "upper_price": 110.0,
                    "levels": 5,
                    "order_size_quote": 10.0,
                },
                "budget": {
                    "quote_budget": 100.0,
                    "base_budget": 0.0,
                    "profit_mode": "compound",
                    "skim_ratio": 0.5,
                },
            },
            "prices": [100.0, 95.0, 90.0, 95.0, 100.0, 105.0, 110.0, 105.0],
        })
        assert r.status_code == 200
        body = r.json()
        assert "initial_equity_quote" in body
        assert "trades_executed" in body

    def test_backtest_auto_prices(self, client):
        r = client.post("/api/v1/backtest", json={
            "config": {
                "market": "BTC-EUR",
                "base_currency": "BTC",
                "quote_currency": "EUR",
                "mode": "simulation",
                "strategy": "static_grid",
                "start_price": 100.0,
                "grid": {
                    "lower_price": 90.0,
                    "upper_price": 110.0,
                    "levels": 5,
                    "order_size_quote": 10.0,
                },
                "budget": {
                    "quote_budget": 100.0,
                    "base_budget": 0.0,
                    "profit_mode": "compound",
                    "skim_ratio": 0.5,
                },
            },
        })
        assert r.status_code == 200


# ── Grid preview ─────────────────────────────────────────────────────


class TestGridPreview:
    def test_profitable_grid(self, client):
        r = client.post("/api/v1/strategy/static-grid/preview", json={
            "grid": {
                "lower_price": 90.0,
                "upper_price": 110.0,
                "levels": 5,
                "order_size_quote": 100.0,
            },
            "fee_rate": 0.001,
        })
        assert r.status_code == 200
        body = r.json()
        assert "is_profitable" in body
        assert "step_size" in body
        assert body["total_trade_paths"] == 4  # levels - 1

    def test_high_fee_unprofitable(self, client):
        r = client.post("/api/v1/strategy/static-grid/preview", json={
            "grid": {
                "lower_price": 99.0,
                "upper_price": 101.0,
                "levels": 2,
                "order_size_quote": 100.0,
            },
            "fee_rate": 0.05,
        })
        assert r.status_code == 200
        assert r.json()["is_profitable"] is False


# ── Market endpoints (mocked) ───────────────────────────────────────


class TestMarketSummary:
    @patch("manager.app.routes.market.requests.get")
    def test_market_summary_ok(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{
            "market": "BTC-EUR",
            "open": "50000",
            "last": "51000",
            "volume": "10",
            "volumeQuote": "500000",
        }]
        mock_get.return_value = mock_resp

        r = client.get("/api/v1/market/summary?market=BTC-EUR")
        assert r.status_code == 200
        body = r.json()
        assert body["market"] == "BTC-EUR"
        assert body["last_price"] == pytest.approx(51000.0)

    @patch("manager.app.routes.market.requests.get")
    def test_market_summary_empty(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        r = client.get("/api/v1/market/summary?market=FAKE-EUR")
        assert r.status_code == 404


class TestListMarkets:
    @patch("manager.app.routes.market.requests.get")
    def test_list_markets(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"market": "BTC-EUR", "base": "BTC", "quote": "EUR", "status": "trading"},
            {"market": "ETH-EUR", "base": "ETH", "quote": "EUR", "status": "trading"},
            {"market": "DOGE-EUR", "base": "DOGE", "quote": "EUR", "status": "halted"},
        ]
        mock_get.return_value = mock_resp

        r = client.get("/api/v1/markets?status=trading")
        assert r.status_code == 200
        markets = r.json()
        assert len(markets) == 2
        assert all(m["status"] == "trading" for m in markets)

    @patch("manager.app.routes.market.requests.get")
    def test_list_markets_api_error(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_get.return_value = mock_resp

        r = client.get("/api/v1/markets")
        assert r.status_code == 502


# ── Balance endpoint (mocked) ───────────────────────────────────────


class TestGetBalance:
    @patch("manager.app.routes.market.requests.get")
    @patch.dict("os.environ", {"BITVAVO_API_KEY": "key", "BITVAVO_API_SECRET": "secret"})
    def test_balance_ok(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"symbol": "BTC", "available": "1.5", "inOrder": "0.1"}]
        mock_get.return_value = mock_resp

        r = client.get("/api/v1/balance?symbol=BTC")
        assert r.status_code == 200
        assert r.json()["available"] == "1.5"

    def test_balance_no_credentials(self, client):
        with patch.dict("os.environ", {"BITVAVO_API_KEY": "", "BITVAVO_API_SECRET": ""}):
            r = client.get("/api/v1/balance?symbol=BTC")
            assert r.status_code == 500

    @patch("manager.app.routes.market.requests.get")
    @patch.dict("os.environ", {"BITVAVO_API_KEY": "key", "BITVAVO_API_SECRET": "secret"})
    def test_balance_401_returns_zeros(self, mock_get, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_get.return_value = mock_resp

        r = client.get("/api/v1/balance?symbol=BTC")
        assert r.status_code == 200
        assert r.json()["available"] == "0"
