"""SQLAlchemy ORM models for the CryptoBot manager database.

Defines the ``User``, ``Agent``, and ``Bot`` tables used by the
manager API for authentication, agent discovery, and bot lifecycle.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
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
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="online")
    approval_status: Mapped[str] = mapped_column(String(32), default="pending")
    capacity: Mapped[int] = mapped_column(Integer, default=5)
    version: Mapped[str] = mapped_column(String(32), default="")
    last_heartbeat: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
