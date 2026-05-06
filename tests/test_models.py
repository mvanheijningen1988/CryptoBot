"""Unit tests for manager.app.models – ORM model defaults and constraints."""
from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from manager.app.models import Agent, Bot, User


class TestUserModel:
    def test_default_role_is_viewer(self, db_session):
        user = User(
            id=str(uuid.uuid4()),
            username="test_user",
            password_hash="fakehash",
        )
        db_session.add(user)
        db_session.commit()
        assert user.role == "viewer"

    def test_default_locale_is_en(self, db_session):
        user = User(
            id=str(uuid.uuid4()),
            username="locale_test",
            password_hash="fakehash",
        )
        db_session.add(user)
        db_session.commit()
        assert user.locale == "en"

    def test_default_must_change_password_is_false(self, db_session):
        user = User(
            id=str(uuid.uuid4()),
            username="pw_test",
            password_hash="fakehash",
        )
        db_session.add(user)
        db_session.commit()
        assert user.must_change_password is False

    def test_duplicate_username_raises(self, db_session):
        u1 = User(id="id1", username="dup", password_hash="h1")
        u2 = User(id="id2", username="dup", password_hash="h2")
        db_session.add(u1)
        db_session.commit()
        db_session.add(u2)
        with pytest.raises(IntegrityError):
            db_session.commit()

    def test_all_fields_roundtrip(self, db_session):
        uid = str(uuid.uuid4())
        user = User(
            id=uid,
            username="full_user",
            password_hash="$2b$12$fakehash",
            role="admin",
            locale="nl",
            must_change_password=True,
        )
        db_session.add(user)
        db_session.commit()

        loaded = db_session.query(User).get(uid)
        assert loaded.username == "full_user"
        assert loaded.role == "admin"
        assert loaded.locale == "nl"
        assert loaded.must_change_password is True


class TestAgentModel:
    def test_default_status(self, db_session):
        agent = Agent(
            id="a1", name="Agent1", base_url="http://localhost:8100"
        )
        db_session.add(agent)
        db_session.commit()
        assert agent.status == "online"

    def test_default_approval_status(self, db_session):
        agent = Agent(
            id="a2", name="Agent2", base_url="http://localhost:8100"
        )
        db_session.add(agent)
        db_session.commit()
        assert agent.approval_status == "pending"

    def test_default_capacity(self, db_session):
        agent = Agent(
            id="a3", name="Agent3", base_url="http://localhost:8100"
        )
        db_session.add(agent)
        db_session.commit()
        assert agent.capacity == 5

    def test_last_heartbeat_has_value(self, db_session):
        agent = Agent(
            id="a4", name="Agent4", base_url="http://localhost:8100"
        )
        db_session.add(agent)
        db_session.commit()
        assert isinstance(agent.last_heartbeat, datetime)


class TestBotModel:
    def test_default_status_is_stopped(self, db_session):
        bot = Bot(
            id="b1",
            name="Bot1",
            strategy_type="static_grid",
            mode="simulation",
            config_json="{}",
        )
        db_session.add(bot)
        db_session.commit()
        assert bot.status == "stopped"

    def test_nullable_assigned_agent(self, db_session):
        bot = Bot(
            id="b2",
            name="Bot2",
            strategy_type="static_grid",
            mode="simulation",
            config_json="{}",
        )
        db_session.add(bot)
        db_session.commit()
        assert bot.assigned_agent_id is None

    def test_timestamps_populated(self, db_session):
        bot = Bot(
            id="b3",
            name="Bot3",
            strategy_type="static_grid",
            mode="simulation",
            config_json="{}",
        )
        db_session.add(bot)
        db_session.commit()
        assert isinstance(bot.created_at, datetime)
        assert isinstance(bot.updated_at, datetime)

    def test_default_metrics_json(self, db_session):
        bot = Bot(
            id="b4",
            name="Bot4",
            strategy_type="static_grid",
            mode="simulation",
            config_json="{}",
        )
        db_session.add(bot)
        db_session.commit()
        assert bot.latest_metrics_json == "{}"
