"""SQLAlchemy ORM models for the CryptoBot manager database.

Defines the ``User``, ``Agent``, and ``Bot`` tables used by the
manager API for authentication, agent discovery, and bot lifecycle.
"""
from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from manager.app.database import Base


class User(Base):
    """Application user with role-based access and locale preference."""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="viewer")
    locale: Mapped[str] = mapped_column(String(8), default="en")
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)


class Agent(Base):
    """Remote agent node that can run bots on behalf of the manager."""
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="online")
    approval_status: Mapped[str] = mapped_column(String(32), default="pending")
    capacity: Mapped[int] = mapped_column(Integer, default=5)
    version: Mapped[str] = mapped_column(String(32), default="")
    uptime_seconds: Mapped[int] = mapped_column(Integer, default=0)
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class Bot(Base):
    """Trading bot instance with its strategy configuration and latest metrics."""
    __tablename__ = "bots"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    strategy_type: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="stopped")
    assigned_agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    config_json: Mapped[str] = mapped_column(Text, nullable=False)
    latest_metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    state_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))


class TradeEvent(Base):
    """Persisted record of an order lifecycle event (placed, filled, cancelled)."""
    __tablename__ = "trade_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    bot_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    bot_name: Mapped[str] = mapped_column(String(128), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(UTC))
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(8), nullable=False)
    quote_amount: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    trade_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    total_equity: Mapped[float] = mapped_column(Float, default=0.0)
    trade_number: Mapped[int] = mapped_column(Integer, default=0)
    level_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    market: Mapped[str] = mapped_column(String(32), default="")
    linked_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
