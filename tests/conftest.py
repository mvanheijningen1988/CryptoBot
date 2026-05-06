"""Shared pytest fixtures for the CryptoBot manager test suite.

Provides an in-memory SQLite database, a fresh SQLAlchemy session, and a
FastAPI ``TestClient`` that overrides the ``get_db`` dependency so every
test runs against an isolated, disposable database.
"""
from __future__ import annotations

import os

# Set env vars before any app modules are imported so JWT_SECRET is
# deterministic and the admin bootstrap uses known credentials.
os.environ.setdefault("JWT_SECRET", "test-secret-key-do-not-use-in-prod")
os.environ.setdefault("ADMIN_USER", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "changeme123")
os.environ.setdefault("DB_URL", "sqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from manager.app.database import Base, get_db
from manager.app.models import User, Agent, Bot  # noqa: F401 – ensure tables registered


@pytest.fixture()
def db_engine():
    """Create a fresh in-memory SQLite engine per test.

    Uses ``StaticPool`` so every connection shares the same in-memory
    database (required for SQLite :memory: to work across sessions).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture()
def db_session(db_engine):
    """Yield a scoped session that rolls back after the test."""
    Session = sessionmaker(bind=db_engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture()
def client(db_engine):
    """Return a ``TestClient`` wired to an in-memory database.

    Patches the ``get_db`` dependency and the module-level
    ``SessionLocal`` used by the startup event and failover thread
    so everything targets the in-memory engine.
    """
    from unittest.mock import patch
    from fastapi.testclient import TestClient
    from manager.app.main import app

    TestSession = sessionmaker(bind=db_engine)

    def _override_get_db():
        db = TestSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db

    with patch("manager.app.main.SessionLocal", TestSession):
        with TestClient(app) as c:
            yield c

    app.dependency_overrides.clear()


@pytest.fixture()
def admin_user(db_session):
    """Insert an admin user and return the ORM instance."""
    from manager.app.auth import hash_password

    user = User(
        id="admin-test-id",
        username="admin",
        password_hash=hash_password("changeme123"),
        role="admin",
        locale="en",
        must_change_password=False,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture()
def viewer_user(db_session):
    """Insert a viewer user and return the ORM instance."""
    from manager.app.auth import hash_password

    user = User(
        id="viewer-test-id",
        username="viewer",
        password_hash=hash_password("viewerpass1"),
        role="viewer",
        locale="en",
        must_change_password=False,
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture()
def admin_token(admin_user):
    """Return a valid JWT for the admin user."""
    from manager.app.auth import create_token

    return create_token(admin_user.id, admin_user.role)


@pytest.fixture()
def viewer_token(viewer_user):
    """Return a valid JWT for the viewer user."""
    from manager.app.auth import create_token

    return create_token(viewer_user.id, viewer_user.role)


@pytest.fixture()
def auth_header(admin_token):
    """Return an Authorization header dict for the admin user."""
    return {"Authorization": f"Bearer {admin_token}"}


@pytest.fixture()
def viewer_header(viewer_token):
    """Return an Authorization header dict for the viewer user."""
    return {"Authorization": f"Bearer {viewer_token}"}


@pytest.fixture()
def sample_agent(db_session):
    """Insert an approved online agent and return the ORM instance."""
    from datetime import datetime

    agent = Agent(
        id="agent-test-id",
        name="Test Agent",
        base_url="http://agent:8100",
        status="online",
        approval_status="approved",
        capacity=5,
        last_heartbeat=datetime.utcnow(),
    )
    db_session.add(agent)
    db_session.commit()
    return agent


@pytest.fixture()
def pending_agent(db_session):
    """Insert a pending agent and return the ORM instance."""
    from datetime import datetime

    agent = Agent(
        id="pending-agent-id",
        name="Pending Agent",
        base_url="http://pending:8100",
        status="pending",
        approval_status="pending",
        capacity=3,
        last_heartbeat=datetime.utcnow(),
    )
    db_session.add(agent)
    db_session.commit()
    return agent


@pytest.fixture()
def sample_bot(db_session, sample_agent):
    """Insert a stopped bot assigned to the sample agent."""
    import json
    from datetime import datetime

    bot = Bot(
        id="bot-test-id",
        name="Test Grid Bot",
        strategy_type="static_grid",
        mode="simulation",
        status="stopped",
        assigned_agent_id=sample_agent.id,
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
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db_session.add(bot)
    db_session.commit()
    return bot
