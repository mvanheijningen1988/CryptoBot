"""Integration tests for the manager FastAPI endpoints.

Uses the ``client`` fixture from conftest which wires a TestClient to an
in-memory SQLite database.  Every test starts with a clean DB.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch, MagicMock

import pytest

from manager.app.auth import create_token, hash_password
from manager.app.models import Agent, Bot, TradeEvent, User


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


def _seed_bot(
    client,
    bot_id="bot-1",
    agent_id=None,
    status="stopped",
    latest_metrics: dict | None = None,
    full_state: dict | None = None,
):
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
            # removed start_price
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
        latest_metrics_json=json.dumps(latest_metrics or {}),
        full_state_json=json.dumps(full_state or {}),
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
            json={"new_password": "newpass1!"},  # NOSONAR
            headers=header,
        )
        assert r.status_code == 200
        # Verify new password works
        r2 = client.post("/api/v1/auth/login", json={"username": "admin", "password": "newpass1!"})  # NOSONAR
        assert r2.status_code == 200

    def test_short_password_rejected(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/auth/change-password",
            json={"new_password": "ab"},  # NOSONAR
            headers=header,
        )
        assert r.status_code == 400

    def test_no_digit_rejected(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/auth/change-password",
            json={"new_password": "abcdefgh!"},  # NOSONAR
            headers=header,
        )
        assert r.status_code == 400

    def test_no_special_char_rejected(self, client):
        _, _, header = _seed_admin(client)
        r = client.post(
            "/api/v1/auth/change-password",
            json={"new_password": "abcdefg1"},  # NOSONAR
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
        r = client.post("/api/v1/auth/change-password", json={"new_password": "newpass1!"})  # NOSONAR
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

    def test_includes_bot_runtime_seconds(self, client):
        _seed_agent(client, "list-b")
        _seed_bot(
            client,
            "list-bot",
            agent_id="list-b",
            status="running",
            latest_metrics={"trade_count": 3, "runtime_seconds": 3723, "quote_balance": 125.0, "base_balance": 1.5},
        )
        r = client.get("/api/v1/agents")
        assert r.status_code == 200
        bot = r.json()[0]["bots"][0]
        assert bot["runtime_seconds"] == 3723


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


class TestBotLogs:
    def test_nonexistent_bot_returns_404(self, client):
        r = client.get("/api/v1/bots/ghost/logs")
        assert r.status_code == 404

    def test_bot_without_agent_returns_400(self, client):
        _seed_bot(client, "logs-bot-no-agent", agent_id=None, status="stopped")
        r = client.get("/api/v1/bots/logs-bot-no-agent/logs")
        assert r.status_code == 400

    def test_bot_with_unapproved_agent_returns_400(self, client):
        _seed_agent(client, "logs-agent-pending", approval="pending", status="pending")
        _seed_bot(client, "logs-bot-pending", agent_id="logs-agent-pending", status="stopped")
        r = client.get("/api/v1/bots/logs-bot-pending/logs")
        assert r.status_code == 400

    @patch("manager.app.routes.bots.requests.get")
    def test_bot_logs_infers_running_agent_when_unassigned(self, mock_get, client):
        _seed_agent(client, "logs-agent-infer", approval="approved", status="online")
        _seed_bot(client, "logs-bot-infer", agent_id=None, status="running")

        bots_resp = MagicMock()
        bots_resp.status_code = 200
        bots_resp.json.return_value = [{"bot_id": "logs-bot-infer", "running": True}]

        logs_resp = MagicMock()
        logs_resp.status_code = 200
        logs_resp.json.return_value = {
            "agent_id": "logs-agent-infer",
            "logs": [{"event_type": "bot_start", "bot_id": "logs-bot-infer", "message": "ok"}],
        }

        def _side_effect(url, *args, **kwargs):  # noqa: ARG001
            if url.endswith("/agent/bots"):
                return bots_resp
            if url.endswith("/agent/logs"):
                return logs_resp
            raise AssertionError(f"Unexpected URL {url}")

        mock_get.side_effect = _side_effect

        r = client.get("/api/v1/bots/logs-bot-infer/logs")
        assert r.status_code == 200
        body = r.json()
        assert body["agent_id"] == "logs-agent-infer"
        assert body["bot_id"] == "logs-bot-infer"

        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "logs-bot-infer").first()
        assert bot is not None
        assert bot.assigned_agent_id == "logs-agent-infer"

    @patch("manager.app.routes.bots.requests.get")
    def test_bot_logs_are_proxied_with_bot_filter(self, mock_get, client):
        _seed_agent(client, "logs-agent-ok", approval="approved", status="online")
        _seed_bot(client, "logs-bot-ok", agent_id="logs-agent-ok", status="running")

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "agent_id": "logs-agent-ok",
            "logs": [{"event_type": "order_opened", "bot_id": "logs-bot-ok", "message": "test"}],
        }
        mock_get.return_value = mock_resp

        r = client.get("/api/v1/bots/logs-bot-ok/logs?limit=123&category=trading")
        assert r.status_code == 200
        body = r.json()
        assert body["bot_id"] == "logs-bot-ok"
        assert body["agent_id"] == "logs-agent-ok"
        assert isinstance(body["logs"], list)
        assert body["logs"][0]["bot_id"] == "logs-bot-ok"

        called_url = mock_get.call_args.args[0]
        called_params = mock_get.call_args.kwargs["params"]
        assert called_url == "http://agent:8100/agent/logs"
        assert called_params["bot_id"] == "logs-bot-ok"
        assert called_params["category"] == "trading"
        assert called_params["limit"] == 123


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
                # removed start_price
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
        assert body["latest_metrics"]["total_equity_quote"] == 1000.0
        assert body["latest_metrics"]["runtime_seconds"] == 0

    def test_create_bot_with_assigned_agent(self, client):
        _seed_agent(client, "create-agent")
        r = client.post("/api/v1/bots", json={
            "name": "Assigned Bot",
            "assigned_agent_id": "create-agent",
            "config": {
                "market": "BTC-EUR",
                "base_currency": "BTC",
                "quote_currency": "EUR",
                "mode": "simulation",
                "strategy": "static_grid",
                "grid": {
                    "lower_price": 48000.0,
                    "upper_price": 52000.0,
                    "levels": 5,
                    "order_size_quote": 100.0,
                },
                "budget": {
                    "quote_budget": 57.0,
                    "base_budget": 0.0,
                    "profit_mode": "compound",
                    "skim_ratio": 0.5,
                },
            },
        })
        assert r.status_code == 200
        body = r.json()
        assert body["assigned_agent_id"] == "create-agent"
        assert body["latest_metrics"]["quote_balance"] == 57.0
        assert body["latest_metrics"]["total_equity_quote"] == 57.0

    def test_create_bot_auto_assigns_available_agent(self, client):
        _seed_agent(client, "create-auto-agent")
        r = client.post("/api/v1/bots", json={
            "name": "Auto Assigned Bot",
            "config": {
                "market": "BTC-EUR",
                "base_currency": "BTC",
                "quote_currency": "EUR",
                "mode": "simulation",
                "strategy": "static_grid",
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
        assert body["assigned_agent_id"] == "create-auto-agent"

    @patch("manager.app.routes.bots.requests.get")
    def test_create_live_bot_rejects_order_size_below_bitvavo_minimum(self, mock_get, client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "market": "BTC-EUR",
                "minOrderInQuoteAsset": "10",
                "minOrderInBaseAsset": "0.0002",
            }
        ]
        mock_get.return_value = mock_response

        r = client.post("/api/v1/bots", json={
            "name": "Live Bot",
            "config": {
                "market": "BTC-EUR",
                "base_currency": "BTC",
                "quote_currency": "EUR",
                "mode": "live",
                "strategy": "static_grid",
                "grid": {
                    "lower_price": 48000.0,
                    "upper_price": 52000.0,
                    "levels": 5,
                    "order_size_quote": 5.0,
                },
                "budget": {
                    "quote_budget": 1000.0,
                    "base_budget": 0.0,
                    "profit_mode": "compound",
                    "skim_ratio": 0.5,
                },
            },
        })

        assert r.status_code == 400
        assert "Minimum required order size is 10.40000000 EUR" in r.json()["detail"]

    @patch("manager.app.routes.bots.requests.get")
    def test_start_live_bot_rejects_order_size_below_bitvavo_minimum(self, mock_get, client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "market": "BTC-EUR",
                "minOrderInQuoteAsset": "10",
                "minOrderInBaseAsset": "0.0002",
            }
        ]
        mock_get.return_value = mock_response

        _seed_agent(client, "start-min-agent")
        db = _get_db(client)
        bot = Bot(
            id="start-min-bot",
            name="Start Min Bot",
            strategy_type="static_grid",
            mode="live",
            status="stopped",
            assigned_agent_id="start-min-agent",
            config_json=json.dumps({
                "market": "BTC-EUR",
                "base_currency": "BTC",
                "quote_currency": "EUR",
                "mode": "live",
                "strategy": "static_grid",
                "grid": {
                    "lower_price": 48000.0,
                    "upper_price": 52000.0,
                    "levels": 5,
                    "order_size_quote": 5.0,
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

        r = client.post("/api/v1/bots/start-min-bot/start", json={"agent_id": "start-min-agent"})

        assert r.status_code == 400
        assert "Minimum required order size is 10.40000000 EUR" in r.json()["detail"]


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

    def test_list_bots_exposes_mark_to_market_dashboard_pnl(self, client):
        _seed_agent(client, "lb-pnl-agent")
        _seed_bot(client, "lb-pnl-bot", agent_id="lb-pnl-agent")
        db = _get_db(client)
        row = db.query(Bot).filter(Bot.id == "lb-pnl-bot").first()
        row.latest_metrics_json = json.dumps({
            "trade_count": 0,
            "realized_pnl_quote": 0.0,
            "unrealized_pnl_quote": 42.5,
            "total_equity_quote": 1042.5,
        })
        db.commit()

        r = client.get("/api/v1/bots")

        assert r.status_code == 200
        assert r.json()[0]["latest_metrics"]["dashboard_pnl_quote"] == pytest.approx(42.5)


class TestEquityHistoryPnl:
    def test_bot_equity_history_returns_mark_to_market_pnl(self, client):
        _seed_bot(client, "eq-pnl-bot")
        db = _get_db(client)
        row = db.query(Bot).filter(Bot.id == "eq-pnl-bot").first()
        row.latest_metrics_json = json.dumps({
            "trade_count": 0,
            "realized_pnl_quote": 0.0,
            "unrealized_pnl_quote": 99.0,
            "total_equity_quote": 1099.0,
        })
        db.commit()

        r = client.get("/api/v1/bots/eq-pnl-bot/equity-history")

        assert r.status_code == 200
        assert r.json()["pnl"] == pytest.approx(99.0)

    def test_total_equity_history_sums_mark_to_market_pnl(self, client):
        _seed_bot(client, "eq-total-a")
        _seed_bot(client, "eq-total-b")
        db = _get_db(client)
        first_row = db.query(Bot).filter(Bot.id == "eq-total-a").first()
        second_row = db.query(Bot).filter(Bot.id == "eq-total-b").first()
        first_row.latest_metrics_json = json.dumps({
            "trade_count": 0,
            "realized_pnl_quote": 0.0,
            "unrealized_pnl_quote": 50.0,
            "total_equity_quote": 1050.0,
        })
        second_row.latest_metrics_json = json.dumps({
            "trade_count": 2,
            "realized_pnl_quote": 7.5,
            "unrealized_pnl_quote": 80.0,
            "total_equity_quote": 1080.0,
        })
        db.commit()

        r = client.get("/api/v1/bots/equity-history/total")

        assert r.status_code == 200
        assert r.json()["pnl"] == pytest.approx(130.0)
        assert "series" in r.json()
        assert isinstance(r.json().get("series"), list)

    def test_live_bot_equity_history_uses_reconstructed_metrics(self, client):
        _seed_bot(client, "eq-live-bot")
        db = _get_db(client)
        row = db.query(Bot).filter(Bot.id == "eq-live-bot").first()
        cfg = json.loads(row.config_json)
        cfg["mode"] = "live"
        row.mode = "live"
        row.config_json = json.dumps(cfg)
        row.latest_metrics_json = json.dumps({
            "price": 12.0,
            "total_equity_quote": 0.0,
            "trade_count": 0,
        })
        db.add(TradeEvent(
            id="eq-live-fill-1",
            bot_id="eq-live-bot",
            bot_name=row.name,
            event_type="order_filled",
            side="buy",
            quote_amount=100.0,
            fill_count=1,
            fee_paid_quote=0.0,
            fee_rate=0.0,
            price=10.0,
            trade_pnl=0.0,
            total_equity=0.0,
            trade_number=1,
            market="BTC-EUR",
        ))
        db.commit()

        r = client.get("/api/v1/bots/eq-live-bot/equity-history")

        assert r.status_code == 200
        assert r.json()["total_equity"] == pytest.approx(1020.0)
        assert r.json()["pnl"] == pytest.approx(20.0)

    def test_live_bot_equity_history_repairs_points_from_fills(self, client):
        _seed_bot(client, "eq-live-points")
        db = _get_db(client)
        row = db.query(Bot).filter(Bot.id == "eq-live-points").first()
        cfg = json.loads(row.config_json)
        cfg["mode"] = "live"
        row.mode = "live"
        row.config_json = json.dumps(cfg)
        row.latest_metrics_json = json.dumps({"price": 12.0})
        db.add(TradeEvent(
            id="eq-live-fill-2",
            bot_id="eq-live-points",
            bot_name=row.name,
            timestamp=datetime(2026, 1, 1, 10, 0, 30),
            event_type="order_filled",
            side="buy",
            quote_amount=100.0,
            fill_count=1,
            fee_paid_quote=0.0,
            fee_rate=0.0,
            price=10.0,
            trade_pnl=0.0,
            total_equity=0.0,
            trade_number=1,
            market="BTC-EUR",
        ))
        db.commit()

        from manager.app.events import EQUITY_HISTORY, EQUITY_HISTORY_LOCK

        with EQUITY_HISTORY_LOCK:
            EQUITY_HISTORY["eq-live-points"] = [
                {"t": "2026-01-01T10:00:00+00:00", "v": 1000.0, "p": 10.0},
                {"t": "2026-01-01T10:01:00+00:00", "v": 900.0, "p": 12.0},
            ]

        r = client.get("/api/v1/bots/eq-live-points/equity-history")

        with EQUITY_HISTORY_LOCK:
            EQUITY_HISTORY.pop("eq-live-points", None)

        assert r.status_code == 200
        assert len(r.json()["points"]) == 2
        assert r.json()["points"][0]["v"] == pytest.approx(1000.0)
        assert r.json()["points"][1]["v"] == pytest.approx(1020.0)

    def test_live_bot_equity_history_backfills_points_when_memory_empty(self, client):
        _seed_bot(client, "eq-live-backfill")
        db = _get_db(client)
        row = db.query(Bot).filter(Bot.id == "eq-live-backfill").first()
        cfg = json.loads(row.config_json)
        cfg["mode"] = "live"
        row.mode = "live"
        row.config_json = json.dumps(cfg)
        row.latest_metrics_json = json.dumps({"price": 12.0})
        db.add(TradeEvent(
            id="eq-live-fill-3",
            bot_id="eq-live-backfill",
            bot_name=row.name,
            timestamp=datetime(2026, 1, 1, 10, 2, 0),
            event_type="order_filled",
            side="buy",
            quote_amount=100.0,
            fill_count=1,
            fee_paid_quote=0.0,
            fee_rate=0.0,
            price=10.0,
            trade_pnl=0.0,
            total_equity=0.0,
            trade_number=1,
            market="BTC-EUR",
        ))
        db.commit()

        from manager.app.events import EQUITY_HISTORY, EQUITY_HISTORY_LOCK

        with EQUITY_HISTORY_LOCK:
            EQUITY_HISTORY.pop("eq-live-backfill", None)

        r = client.get("/api/v1/bots/eq-live-backfill/equity-history")

        assert r.status_code == 200
        assert len(r.json()["points"]) >= 2
        assert r.json()["points"][0]["v"] == pytest.approx(1000.0)
        assert r.json()["points"][-1]["v"] == pytest.approx(1000.0)

    def test_live_bot_equity_history_recomputes_realized_from_fills(self, client):
        _seed_bot(client, "eq-live-recompute")
        db = _get_db(client)
        row = db.query(Bot).filter(Bot.id == "eq-live-recompute").first()
        cfg = json.loads(row.config_json)
        cfg["mode"] = "live"
        row.mode = "live"
        row.config_json = json.dumps(cfg)
        row.latest_metrics_json = json.dumps({"price": 12.0})

        db.add(TradeEvent(
            id="eq-live-recompute-buy",
            bot_id="eq-live-recompute",
            bot_name=row.name,
            timestamp=datetime(2026, 1, 1, 10, 0, 0),
            event_type="order_filled",
            side="buy",
            quote_amount=100.0,
            fill_count=1,
            fee_paid_quote=0.0,
            fee_rate=0.0,
            price=10.0,
            trade_pnl=0.0,
            total_equity=0.0,
            trade_number=1,
            market="BTC-EUR",
        ))
        db.add(TradeEvent(
            id="eq-live-recompute-sell",
            bot_id="eq-live-recompute",
            bot_name=row.name,
            timestamp=datetime(2026, 1, 1, 10, 1, 0),
            event_type="order_filled",
            side="sell",
            quote_amount=110.0,
            fill_count=1,
            fee_paid_quote=0.0,
            fee_rate=0.0,
            price=11.0,
            trade_pnl=110.0,
            total_equity=0.0,
            trade_number=2,
            market="BTC-EUR",
        ))
        db.commit()

        r = client.get("/api/v1/bots/eq-live-recompute/equity-history")

        assert r.status_code == 200
        assert r.json()["pnl"] == pytest.approx(10.0)
        assert r.json()["total_equity"] == pytest.approx(1010.0)

    def test_live_bot_equity_history_ignores_persisted_equity_snapshots(self, client):
        _seed_bot(client, "eq-live-persistent-rebuild")
        db = _get_db(client)
        row = db.query(Bot).filter(Bot.id == "eq-live-persistent-rebuild").first()
        cfg = json.loads(row.config_json)
        cfg["mode"] = "live"
        row.mode = "live"
        row.config_json = json.dumps(cfg)
        row.latest_metrics_json = json.dumps({"price": 10.0})

        db.add(TradeEvent(
            id="eq-live-persistent-fill",
            bot_id="eq-live-persistent-rebuild",
            bot_name=row.name,
            timestamp=datetime(2026, 1, 1, 10, 5, 0),
            event_type="order_filled",
            side="buy",
            quote_amount=100.0,
            fill_count=1,
            fee_paid_quote=0.0,
            fee_rate=0.0,
            price=10.0,
            trade_pnl=0.0,
            total_equity=9999.0,
            trade_number=1,
            market="BTC-EUR",
        ))
        db.commit()

        from manager.app.events import EQUITY_HISTORY, EQUITY_HISTORY_LOCK

        with EQUITY_HISTORY_LOCK:
            EQUITY_HISTORY.pop("eq-live-persistent-rebuild", None)

        r = client.get("/api/v1/bots/eq-live-persistent-rebuild/equity-history")

        assert r.status_code == 200
        assert len(r.json()["points"]) >= 2
        assert r.json()["points"][0]["v"] == pytest.approx(1000.0)
        assert r.json()["points"][-1]["v"] == pytest.approx(1000.0)


class TestStartBot:
    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    def test_start_with_agent(self, mock_post, client):
        _seed_agent(client, "start-agent")
        _seed_bot(client, "start-bot", agent_id="start-agent")
        r = client.post("/api/v1/bots/start-bot/start", json={"agent_id": "start-agent"})
        assert r.status_code == 200
        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "start-bot").first()
        assert bot.status == "initializing"

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


class TestDeleteBot:
    @patch("manager.app.routes.bots.delete_trade_events_for_bot")
    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    def test_delete_running_bot_delete_open_orders_calls_agent_prepare_delete(self, mock_post, mock_delete_events, client):
        _seed_agent(client, "delete-agent")
        _seed_bot(client, "delete-running-bot", agent_id="delete-agent", status="running")

        r = client.delete("/api/v1/bots/delete-running-bot")

        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["delete_mode"] == "delete_open_orders"
        mock_post.assert_called_once_with(
            "http://agent:8100/agent/bots/delete-running-bot/prepare-delete",
            {"bot_id": "delete-running-bot", "delete_mode": "delete_open_orders"},
        )
        mock_delete_events.assert_called_once_with("delete-running-bot")

        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "delete-running-bot").first()
        assert bot is None

    @patch("manager.app.routes.bots.delete_trade_events_for_bot")
    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    def test_delete_running_bot_as_is_calls_agent_prepare_delete(self, mock_post, mock_delete_events, client):
        _seed_agent(client, "delete-agent-as-is")
        _seed_bot(client, "delete-running-bot-as-is", agent_id="delete-agent-as-is", status="running")

        r = client.request(
            "DELETE",
            "/api/v1/bots/delete-running-bot-as-is",
            json={"delete_mode": "delete_as_is"},
        )

        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["delete_mode"] == "delete_as_is"
        mock_post.assert_called_once_with(
            "http://agent:8100/agent/bots/delete-running-bot-as-is/prepare-delete",
            {"bot_id": "delete-running-bot-as-is", "delete_mode": "delete_as_is"},
        )
        mock_delete_events.assert_called_once_with("delete-running-bot-as-is")

        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "delete-running-bot-as-is").first()
        assert bot is None

    @patch("manager.app.routes.bots.delete_trade_events_for_bot")
    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    def test_delete_running_bot_transform_to_quote_calls_agent_prepare_delete(self, mock_post, mock_delete_events, client):
        _seed_agent(client, "delete-agent-quote")
        _seed_bot(client, "delete-running-bot-quote", agent_id="delete-agent-quote", status="running")

        r = client.request(
            "DELETE",
            "/api/v1/bots/delete-running-bot-quote",
            json={"delete_mode": "transform_to_quote"},
        )

        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert r.json()["delete_mode"] == "transform_to_quote"
        mock_post.assert_called_once_with(
            "http://agent:8100/agent/bots/delete-running-bot-quote/prepare-delete",
            {"bot_id": "delete-running-bot-quote", "delete_mode": "transform_to_quote"},
        )
        mock_delete_events.assert_called_once_with("delete-running-bot-quote")

    @patch("manager.app.routes.bots.delete_trade_events_for_bot")
    def test_delete_open_orders_rejected_without_agent_when_saved_open_orders_exist(self, mock_delete_events, client):
        _seed_bot(
            client,
            "delete-no-agent-open-orders",
            agent_id=None,
            status="stopped",
            full_state={
                "runner_state": {
                    "open_orders": {"0": "buy", "1": "sell"},
                }
            },
        )

        r = client.delete("/api/v1/bots/delete-no-agent-open-orders")

        assert r.status_code == 409
        assert "cancel open orders" in r.json()["detail"]
        mock_delete_events.assert_not_called()

    @patch("manager.app.routes.bots.delete_trade_events_for_bot")
    def test_delete_open_orders_allowed_without_agent_when_no_saved_open_orders(self, mock_delete_events, client):
        _seed_bot(
            client,
            "delete-no-agent-empty",
            agent_id=None,
            status="stopped",
            full_state={"runner_state": {"open_orders": {}}},
        )

        r = client.delete("/api/v1/bots/delete-no-agent-empty")

        assert r.status_code == 200
        assert r.json()["ok"] is True
        mock_delete_events.assert_called_once_with("delete-no-agent-empty")


class TestMoveBot:
    def test_move_stopped_bot_reassigns_and_logs(self, client):
        _seed_agent(client, "move-src")
        _seed_agent(client, "move-dst")
        _seed_bot(client, "move-bot", agent_id="move-src", status="stopped")

        r = client.post("/api/v1/bots/move-bot/move", json={"agent_id": "move-dst"})
        assert r.status_code == 200

        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "move-bot").first()
        assert bot is not None
        assert bot.assigned_agent_id == "move-dst"
        assert bot.status == "stopped"

        ev = client.get("/api/v1/agent-events")
        assert ev.status_code == 200
        kinds = [e["event_type"] for e in ev.json()]
        assert "bot_moved_in" in kinds
        assert "bot_moved_out" in kinds

    @patch("manager.app.routes.bots.post_json", side_effect=[(True, "ok"), (True, "ok")])
    def test_move_active_bot_stops_then_starts(self, mock_post, client):
        _seed_agent(client, "move-src-active")
        _seed_agent(client, "move-dst-active")
        _seed_bot(
            client,
            "move-active-bot",
            agent_id="move-src-active",
            status="running",
            full_state={
                "runner_state": {
                    "level_index": 3,
                    "open_orders": {"3": "order-3"},
                    "filled_buys": [1, 2],
                    "filled_amounts": {"3": 42.5},
                    "price": 101.25,
                    "quote_balance": 850.0,
                    "base_balance": 1.75,
                    "initial_equity": 1000.0,
                    "realized_pnl": 12.5,
                    "skimmed_quote": 0.0,
                    "trade_count": 9,
                }
            },
        )

        r = client.post("/api/v1/bots/move-active-bot/move", json={"agent_id": "move-dst-active"})
        assert r.status_code == 200
        assert mock_post.call_count == 2
        start_payload = mock_post.call_args_list[1].args[1]
        assert start_payload["runner_state"]["level_index"] == 3
        assert start_payload["runner_state"]["trade_count"] == 9

        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "move-active-bot").first()
        assert bot is not None
        assert bot.assigned_agent_id == "move-dst-active"
        assert bot.status == "initializing"

    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    @patch("manager.app.routes.bots.send_agent_command_ws", return_value=(True, "ok", None))
    def test_move_active_bot_prefers_websocket(self, mock_ws, mock_post, client):
        _seed_agent(client, "move-src-ws")
        _seed_agent(client, "move-dst-ws")
        _seed_bot(client, "move-ws-bot", agent_id="move-src-ws", status="running")

        r = client.post("/api/v1/bots/move-ws-bot/move", json={"agent_id": "move-dst-ws"})

        assert r.status_code == 200
        assert mock_ws.call_count == 2
        mock_post.assert_not_called()

    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    @patch("manager.app.routes.bots.send_agent_command_ws", return_value=(False, "ws_timeout", None))
    def test_move_active_bot_ws_timeout_falls_back_http(self, mock_ws, mock_post, client):
        _seed_agent(client, "move-src-fallback")
        _seed_agent(client, "move-dst-fallback")
        _seed_bot(client, "move-fallback-bot", agent_id="move-src-fallback", status="running")

        r = client.post("/api/v1/bots/move-fallback-bot/move", json={"agent_id": "move-dst-fallback"})

        assert r.status_code == 200
        assert mock_ws.call_count == 2
        assert mock_post.call_count == 2


class TestWsFirstCommandDispatch:
    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    @patch("manager.app.routes.bots.send_agent_command_ws", return_value=(True, "started", None))
    def test_start_bot_prefers_websocket(self, mock_ws, mock_post, client):
        _seed_agent(client, "start-ws-agent")
        _seed_bot(client, "start-ws-bot", agent_id="start-ws-agent", status="stopped")

        r = client.post(
            "/api/v1/bots/start-ws-bot/start",
            json={"agent_id": "start-ws-agent"},
        )

        assert r.status_code == 200
        mock_ws.assert_called_once()
        mock_post.assert_not_called()

    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    @patch("manager.app.routes.bots.send_agent_command_ws", return_value=(False, "ws_timeout", None))
    def test_start_bot_ws_timeout_falls_back_http(self, mock_ws, mock_post, client):
        _seed_agent(client, "start-http-agent")
        _seed_bot(client, "start-http-bot", agent_id="start-http-agent", status="stopped")

        r = client.post(
            "/api/v1/bots/start-http-bot/start",
            json={"agent_id": "start-http-agent"},
        )

        assert r.status_code == 200
        mock_ws.assert_called_once()
        mock_post.assert_called_once()

    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    @patch("manager.app.routes.bots.send_agent_command_ws", return_value=(True, "stopped", None))
    def test_stop_bot_prefers_websocket(self, mock_ws, mock_post, client):
        _seed_agent(client, "stop-ws-agent")
        _seed_bot(client, "stop-ws-bot", agent_id="stop-ws-agent", status="running")

        r = client.post("/api/v1/bots/stop-ws-bot/stop")

        assert r.status_code == 200
        mock_ws.assert_called_once()
        mock_post.assert_not_called()

    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    @patch("manager.app.routes.bots.send_agent_command_ws", return_value=(False, "ws_timeout", None))
    def test_stop_bot_ws_timeout_falls_back_http(self, mock_ws, mock_post, client):
        _seed_agent(client, "stop-http-agent")
        _seed_bot(client, "stop-http-bot", agent_id="stop-http-agent", status="running")

        r = client.post("/api/v1/bots/stop-http-bot/stop")

        assert r.status_code == 200
        mock_ws.assert_called_once()
        mock_post.assert_called_once()

    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    @patch(
        "manager.app.routes.bots.send_agent_command_ws",
        return_value=(True, "synced", {"details": {"matched_levels": [1, 2]}}),
    )
    def test_sync_bot_prefers_websocket_and_returns_details(self, mock_ws, mock_post, client):
        _seed_agent(client, "sync-ws-agent")
        _seed_bot(client, "sync-ws-bot", agent_id="sync-ws-agent", status="running")

        r = client.post("/api/v1/bots/sync-ws-bot/sync")

        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["message"] == "synced"
        assert body["details"]["details"]["matched_levels"] == [1, 2]
        mock_ws.assert_called_once()
        mock_post.assert_not_called()

    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    @patch("manager.app.routes.bots.send_agent_command_ws", return_value=(False, "ws_timeout", None))
    def test_sync_bot_ws_timeout_falls_back_http(self, mock_ws, mock_post, client):
        _seed_agent(client, "sync-http-agent")
        _seed_bot(client, "sync-http-bot", agent_id="sync-http-agent", status="running")

        r = client.post("/api/v1/bots/sync-http-bot/sync")

        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["message"] == "ok"
        assert "details" not in body
        mock_ws.assert_called_once()
        mock_post.assert_called_once()


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

    @patch("manager.app.routes.bots.post_json", return_value=(True, "ok"))
    @patch("manager.app.routes.bots.send_agent_command_ws", return_value=(True, "budget_updated", None))
    def test_update_budget_running_bot_prefers_websocket(self, mock_ws, mock_post, client):
        _seed_agent(client, "brun-ws-agent")
        _seed_bot(client, "brun-ws-bot", agent_id="brun-ws-agent", status="running")

        r = client.post(
            "/api/v1/bots/brun-ws-bot/budget",
            json={"quote_budget": 3100.0, "base_budget": 1.25},
        )

        assert r.status_code == 200
        mock_ws.assert_called_once()
        mock_post.assert_not_called()


class TestOpenOrders:
    def test_reconstructs_from_saved_state_when_not_running(self, client):
        _seed_bot(
            client,
            "open-orders-bot",
            status="stopped",
            full_state={
                "runner_state": {
                    "open_orders": {"1": "buy", "4": "sell"},
                    "filled_amounts": {"1": 25.0, "4": 25.0},
                }
            },
        )

        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "open-orders-bot").first()
        assert bot is not None

        r = client.get("/api/v1/bots/open-orders-bot/open-orders")
        assert r.status_code == 200
        body = r.json()
        assert body["bot_id"] == "open-orders-bot"
        assert [order["side"] for order in body["orders"]] == ["buy", "sell"]
        assert body["orders"][0]["level"] == 1
        assert body["orders"][1]["level"] == 4


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
        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "met-bot").first()
        assert bot.status == "running"

    def test_push_metrics_updates_assigned_agent(self, client):
        _seed_agent(client, "met-assign")
        _seed_bot(client, "met-assign-bot", agent_id=None)

        r = client.post(
            "/api/v1/agents/met-assign/bots/met-assign-bot/metrics",
            json={
                "snapshot": {
                    "bot_id": "met-assign-bot",
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

        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "met-assign-bot").first()
        assert bot is not None
        assert bot.assigned_agent_id == "met-assign"

    def test_push_metrics_persists_full_state_separately_from_flags(self, client):
        _seed_agent(client, "met-full-state")
        _seed_bot(
            client,
            "met-full-state-bot",
            agent_id="met-full-state",
            status="running",
            latest_metrics={"trade_count": 2},
        )
        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "met-full-state-bot").first()
        bot.state_json = json.dumps({"manual_stop": True})
        db.commit()

        r = client.post(
            "/api/v1/agents/met-full-state/bots/met-full-state-bot/metrics",
            json={
                "snapshot": {
                    "bot_id": "met-full-state-bot",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "price": 100.0,
                    "quote_balance": 900.0,
                    "base_balance": 1.0,
                    "base_value_in_quote": 100.0,
                    "total_equity_quote": 1000.0,
                    "realized_pnl_quote": 0.0,
                    "unrealized_pnl_quote": 0.0,
                    "skimmed_quote": 0.0,
                    "trade_count": 2,
                    "status": "running",
                },
                "runner_state": {
                    "level_index": 4,
                    "open_orders": {"4": "order-4"},
                    "filled_buys": [1, 2],
                    "filled_amounts": {"4": 50.0},
                    "price": 100.0,
                    "quote_balance": 900.0,
                    "base_balance": 1.0,
                    "initial_equity": 1000.0,
                    "realized_pnl": 0.0,
                    "skimmed_quote": 0.0,
                    "trade_count": 2,
                },
            },
        )
        assert r.status_code == 200

        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "met-full-state-bot").first()
        assert bot is not None
        assert json.loads(bot.state_json)["manual_stop"] is True
        full_state = json.loads(bot.full_state_json)
        assert full_state["runner_state"]["level_index"] == 4
        assert full_state["snapshot"]["trade_count"] == 2

    def test_push_metrics_ignored_for_manually_stopped_bot(self, client):
        _seed_agent(client, "met-stop-agent")
        _seed_bot(client, "met-stop-bot", agent_id=None, status="stopped")
        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "met-stop-bot").first()
        bot.state_json = json.dumps({"manual_stop": True})
        db.commit()

        r = client.post(
            "/api/v1/agents/met-stop-agent/bots/met-stop-bot/metrics",
            json={
                "snapshot": {
                    "bot_id": "met-stop-bot",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "price": 100.0,
                    "quote_balance": 900.0,
                    "base_balance": 1.0,
                    "base_value_in_quote": 100.0,
                    "total_equity_quote": 1000.0,
                    "realized_pnl_quote": 0.0,
                    "unrealized_pnl_quote": 0.0,
                    "skimmed_quote": 0.0,
                    "trade_count": 1,
                    "status": "running",
                },
            },
        )

        assert r.status_code == 200
        assert r.json().get("ignored") == "bot_manually_stopped"

        db = _get_db(client)
        bot = db.query(Bot).filter(Bot.id == "met-stop-bot").first()
        assert bot is not None
        assert bot.status == "stopped"
        assert bot.assigned_agent_id is None

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

    def test_push_metrics_links_sell_to_previous_buy_level(self, client):
        _seed_agent(client, "met3-agent")
        _seed_bot(client, "met3-bot", agent_id="met3-agent")
        from manager.app.main import SessionLocal as TestSessionLocal

        with patch("manager.app.events.SessionLocal", TestSessionLocal), patch("manager.app.database.SessionLocal", TestSessionLocal):
            buy_resp = client.post(
                "/api/v1/agents/met3-agent/bots/met3-bot/metrics",
                json={
                    "snapshot": {
                        "bot_id": "met3-bot",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "price": 1.0,
                        "quote_balance": 900.0,
                        "base_balance": 100.0,
                        "base_value_in_quote": 100.0,
                        "total_equity_quote": 1000.0,
                        "realized_pnl_quote": 0.0,
                        "unrealized_pnl_quote": 0.0,
                        "skimmed_quote": 0.0,
                        "trade_count": 1,
                        "status": "running"
                    },
                    "trade_events": [
                        {
                            "event_type": "order_filled",
                            "side": "buy",
                            "quote_amount": 100.0,
                            "price": 1.0,
                            "level_index": 1,
                            "trade_pnl": 0.0,
                            "total_equity": 1000.0,
                            "trade_number": 1
                        }
                    ]
                },
            )
            assert buy_resp.status_code == 200

            sell_resp = client.post(
                "/api/v1/agents/met3-agent/bots/met3-bot/metrics",
                json={
                    "snapshot": {
                        "bot_id": "met3-bot",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "price": 2.0,
                        "quote_balance": 900.0,
                        "base_balance": 100.0,
                        "base_value_in_quote": 200.0,
                        "total_equity_quote": 1100.0,
                        "realized_pnl_quote": 0.0,
                        "unrealized_pnl_quote": 100.0,
                        "skimmed_quote": 0.0,
                        "trade_count": 1,
                        "status": "running"
                    },
                    "trade_events": [
                        {
                            "event_type": "order_placed",
                            "side": "sell",
                            "quote_amount": 100.0,
                            "price": 2.0,
                            "level_index": 2,
                            "trade_pnl": 0.0,
                            "total_equity": 1100.0,
                            "trade_number": 1
                        }
                    ]
                },
            )
            assert sell_resp.status_code == 200

            events = client.get("/api/v1/trade-events?bot_id=met3-bot")
            assert events.status_code == 200
            sell_event = next(ev for ev in events.json() if ev["event_type"] == "order_placed" and ev["side"] == "sell")

            detail = client.get(f"/api/v1/trade-events/{sell_event['id']}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["linked_order"] is not None
            assert body["linked_order"]["side"] == "buy"
            assert body["linked_order"]["level_index"] == 1

    def test_trade_event_detail_includes_realized_pair_pnl(self, client):
        _seed_agent(client, "met4-agent")
        _seed_bot(client, "met4-bot", agent_id="met4-agent")
        from manager.app.main import SessionLocal as TestSessionLocal

        with patch("manager.app.events.SessionLocal", TestSessionLocal), patch("manager.app.database.SessionLocal", TestSessionLocal):
            buy_resp = client.post(
                "/api/v1/agents/met4-agent/bots/met4-bot/metrics",
                json={
                    "snapshot": {
                        "bot_id": "met4-bot",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "price": 1.0,
                        "quote_balance": 900.0,
                        "base_balance": 100.0,
                        "base_value_in_quote": 100.0,
                        "total_equity_quote": 1000.0,
                        "realized_pnl_quote": 0.0,
                        "unrealized_pnl_quote": 0.0,
                        "skimmed_quote": 0.0,
                        "trade_count": 1,
                        "status": "running"
                    },
                    "trade_events": [
                        {
                            "event_type": "order_filled",
                            "side": "buy",
                            "quote_amount": 100.0,
                            "price": 1.0,
                            "level_index": 1,
                            "trade_pnl": 0.0,
                            "total_equity": 1000.0,
                            "trade_number": 1
                        }
                    ]
                },
            )
            assert buy_resp.status_code == 200

            sell_resp = client.post(
                "/api/v1/agents/met4-agent/bots/met4-bot/metrics",
                json={
                    "snapshot": {
                        "bot_id": "met4-bot",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "price": 1.2,
                        "quote_balance": 1020.0,
                        "base_balance": 0.0,
                        "base_value_in_quote": 0.0,
                        "total_equity_quote": 1020.0,
                        "realized_pnl_quote": 19.45,
                        "unrealized_pnl_quote": 0.0,
                        "skimmed_quote": 0.0,
                        "trade_count": 2,
                        "status": "running"
                    },
                    "trade_events": [
                        {
                            "event_type": "order_filled",
                            "side": "sell",
                            "quote_amount": 100.0,
                            "price": 1.2,
                            "level_index": 2,
                            "trade_pnl": 19.45,
                            "total_equity": 1020.0,
                            "trade_number": 2
                        }
                    ]
                },
            )
            assert sell_resp.status_code == 200

            events = client.get("/api/v1/trade-events?bot_id=met4-bot")
            assert events.status_code == 200
            sell_event = next(ev for ev in events.json() if ev["event_type"] == "order_filled" and ev["side"] == "sell")

            detail = client.get(f"/api/v1/trade-events/{sell_event['id']}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["pair_metrics"] is not None
            assert body["pair_metrics"]["quantity_base"] == pytest.approx(100.0)
            assert body["pair_metrics"]["gross_profit_quote"] == pytest.approx(20.0)
            assert body["pair_metrics"]["total_fees_quote"] == pytest.approx(0.0)
            assert body["pair_metrics"]["realized_pnl_quote"] == pytest.approx(20.0)
            assert body["pair_metrics"]["fee_rate"] == pytest.approx(0.0)

    def test_order_fill_updates_existing_order_line_by_order_id(self, client):
        _seed_agent(client, "met5-agent")
        _seed_bot(client, "met5-bot", agent_id="met5-agent")
        from manager.app.main import SessionLocal as TestSessionLocal

        with patch("manager.app.events.SessionLocal", TestSessionLocal), patch("manager.app.database.SessionLocal", TestSessionLocal):
            placed_resp = client.post(
                "/api/v1/agents/met5-agent/bots/met5-bot/metrics",
                json={
                    "snapshot": {
                        "bot_id": "met5-bot",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "price": 1.0,
                        "quote_balance": 900.0,
                        "base_balance": 100.0,
                        "base_value_in_quote": 100.0,
                        "total_equity_quote": 1000.0,
                        "realized_pnl_quote": 0.0,
                        "unrealized_pnl_quote": 0.0,
                        "skimmed_quote": 0.0,
                        "trade_count": 1,
                        "status": "running"
                    },
                    "trade_events": [
                        {
                            "event_type": "order_placed",
                            "order_id": "ord-abc",
                            "side": "buy",
                            "quote_amount": 100.0,
                            "price": 1.0,
                            "level_index": 1,
                            "trade_pnl": 0.0,
                            "total_equity": 1000.0,
                            "trade_number": 1
                        }
                    ]
                },
            )
            assert placed_resp.status_code == 200

            filled_resp = client.post(
                "/api/v1/agents/met5-agent/bots/met5-bot/metrics",
                json={
                    "snapshot": {
                        "bot_id": "met5-bot",
                        "timestamp": "2026-01-01T00:00:01Z",
                        "price": 1.1,
                        "quote_balance": 910.0,
                        "base_balance": 90.0,
                        "base_value_in_quote": 99.0,
                        "total_equity_quote": 1009.0,
                        "realized_pnl_quote": 9.0,
                        "unrealized_pnl_quote": 0.0,
                        "skimmed_quote": 0.0,
                        "trade_count": 2,
                        "status": "running"
                    },
                    "trade_events": [
                        {
                            "event_type": "order_filled",
                            "order_id": "ord-abc",
                            "side": "buy",
                            "quote_amount": 100.0,
                            "price": 1.0,
                            "level_index": 1,
                            "trade_pnl": 9.0,
                            "total_equity": 1009.0,
                            "trade_number": 2
                        }
                    ]
                },
            )
            assert filled_resp.status_code == 200

            events = client.get("/api/v1/trade-events?bot_id=met5-bot")
            assert events.status_code == 200
            rows = events.json()
            assert len(rows) == 1
            assert rows[0]["event_type"] == "order_filled"
            assert rows[0]["order_id"] == "ord-abc"

    def test_trade_event_persists_fee_fields(self, client):
        _seed_agent(client, "met6-agent")
        _seed_bot(client, "met6-bot", agent_id="met6-agent")
        from manager.app.main import SessionLocal as TestSessionLocal

        with patch("manager.app.events.SessionLocal", TestSessionLocal), patch("manager.app.database.SessionLocal", TestSessionLocal):
            resp = client.post(
                "/api/v1/agents/met6-agent/bots/met6-bot/metrics",
                json={
                    "snapshot": {
                        "bot_id": "met6-bot",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "price": 1.0,
                        "quote_balance": 900.0,
                        "base_balance": 100.0,
                        "base_value_in_quote": 100.0,
                        "total_equity_quote": 1000.0,
                        "realized_pnl_quote": 0.0,
                        "unrealized_pnl_quote": 0.0,
                        "skimmed_quote": 0.0,
                        "trade_count": 1,
                        "status": "running"
                    },
                    "trade_events": [
                        {
                            "event_type": "order_filled",
                            "order_id": "ord-fee-1",
                            "side": "buy",
                            "quote_amount": 100.0,
                            "fill_count": 3,
                            "fee_paid_quote": 0.15,
                            "fee_rate": 0.0015,
                            "price": 1.0,
                            "level_index": 1,
                            "trade_pnl": 0.0,
                            "total_equity": 1000.0,
                            "trade_number": 1
                        }
                    ]
                },
            )
            assert resp.status_code == 200

            rows = client.get("/api/v1/trade-events?bot_id=met6-bot")
            assert rows.status_code == 200
            event = rows.json()[0]
            assert event["fee_paid_quote"] == pytest.approx(0.15)
            assert event["fee_rate"] == pytest.approx(0.0015)
            assert event["fill_count"] == 3

            detail = client.get(f"/api/v1/trade-events/{event['id']}")
            assert detail.status_code == 200
            body = detail.json()
            assert body["fee_paid_quote"] == pytest.approx(0.15)
            assert body["fee_rate"] == pytest.approx(0.0015)
            assert body["fill_count"] == 3


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
                # removed start_price
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
                # removed start_price
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
        assert body["trades"][0]["level"] == 0

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


class TestMarketFees:
    @patch.dict("os.environ", {"BITVAVO_API_KEY": "key", "BITVAVO_API_SECRET": "secret"})
    @patch("manager.app.routes.market.websocket.create_connection")
    def test_market_fees_ok(self, mock_create_connection, client):
        class FakeWS:
            def __init__(self, response_payload):
                self._response_payload = response_payload
                self.sent = []
                self._queue = []

            def send(self, raw):
                msg = json.loads(raw)
                self.sent.append(msg)
                req_id = msg.get("requestId")
                if req_id is None:
                    return
                if msg.get("action") == "authenticate":
                    self._queue.append({"requestId": req_id, "authenticated": True})
                else:
                    self._queue.append({"requestId": req_id, "response": self._response_payload})

            def recv(self):
                if self._queue:
                    return json.dumps(self._queue.pop(0))
                return json.dumps({"requestId": 1, "response": {}})

            def close(self):
                return None

        mock_create_connection.side_effect = [
            FakeWS({"tier": "0", "volume": "10000.00", "maker": "0.0015", "taker": "0.0025"}),
            FakeWS({"fees": {"maker": "0.0016", "taker": "0.0026", "volume": "12000.00"}}),
        ]

        r = client.get("/api/v1/market/fees?market=BTC-EUR")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is True
        assert body["market"] == "BTC-EUR"
        assert body["market_maker_fee_rate"] == pytest.approx(0.0015)
        assert body["market_taker_fee_rate"] == pytest.approx(0.0025)
        assert body["applied_fee_rate"] == pytest.approx(0.0015)
        assert body["applied_fee_type"] == "maker"

    @patch.dict("os.environ", {"BITVAVO_API_KEY": "", "BITVAVO_API_SECRET": ""})
    def test_market_fees_without_credentials_returns_unavailable(self, client):
        r = client.get("/api/v1/market/fees?market=BTC-EUR")
        assert r.status_code == 200
        body = r.json()
        assert body["available"] is False
        assert body["applied_fee_rate"] == pytest.approx(0.0)
