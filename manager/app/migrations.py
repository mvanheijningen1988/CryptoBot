"""Lightweight SQLite migrations for the manager database.

Each migration function is idempotent and safe to re-run.  They are
called once during application startup.
"""
from __future__ import annotations

from sqlalchemy import Engine


def run_migrations(engine: Engine) -> None:
    """
    Execute all pending schema migrations against *engine*.

    :param engine: The SQLAlchemy engine to run migrations on.
    """
    _ensure_agent_approval_column(engine)
    _ensure_agent_version_column(engine)
    _ensure_users_table(engine)
    _drop_agent_name_column(engine)
    _ensure_bot_state_column(engine)
    _ensure_trade_events_table(engine)
    _ensure_agent_uptime_column(engine)
    _add_trade_event_columns(engine)


def _ensure_agent_approval_column(engine: Engine) -> None:
    """Add ``approval_status`` column to agents if missing."""
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(agents)").fetchall()
            columns = {row[1] for row in rows}
            if "approval_status" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE agents ADD COLUMN approval_status VARCHAR(32) DEFAULT 'pending'"
                )
                conn.exec_driver_sql(
                    "UPDATE agents SET approval_status='approved' WHERE approval_status IS NULL"
                )
                conn.commit()
        except Exception:
            pass


def _ensure_agent_version_column(engine: Engine) -> None:
    """Add ``version`` column to agents if missing."""
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(agents)").fetchall()
            columns = {row[1] for row in rows}
            if "version" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE agents ADD COLUMN version VARCHAR(32) DEFAULT ''"
                )
                conn.commit()
        except Exception:
            pass


def _ensure_users_table(engine: Engine) -> None:
    """Create the ``users`` table if it does not yet exist (SQLite only)."""
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchall()
            if not rows:
                conn.exec_driver_sql(
                    """CREATE TABLE users (
                        id VARCHAR(64) PRIMARY KEY,
                        username VARCHAR(128) UNIQUE NOT NULL,
                        password_hash VARCHAR(255) NOT NULL,
                        role VARCHAR(32) DEFAULT 'viewer',
                        locale VARCHAR(8) DEFAULT 'en',
                        must_change_password BOOLEAN DEFAULT 0
                    )"""
                )
                conn.commit()
        except Exception:
            pass


def _drop_agent_name_column(engine: Engine) -> None:
    """Remove the ``name`` column from agents if it exists."""
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(agents)").fetchall()
            columns = {row[1] for row in rows}
            if "name" in columns:
                conn.exec_driver_sql("ALTER TABLE agents DROP COLUMN name")
                conn.commit()
        except Exception:
            pass


def _ensure_bot_state_column(engine: Engine) -> None:
    """Add ``state_json`` column to bots if missing."""
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(bots)").fetchall()
            columns = {row[1] for row in rows}
            if "state_json" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE bots ADD COLUMN state_json TEXT DEFAULT '{}'"
                )
                conn.commit()
        except Exception:
            pass


def _ensure_trade_events_table(engine: Engine) -> None:
    """Create the ``trade_events`` table if it does not yet exist."""
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_events'"
            ).fetchall()
            if not rows:
                conn.exec_driver_sql(
                    """CREATE TABLE trade_events (
                        id VARCHAR(64) PRIMARY KEY,
                        order_id VARCHAR(128),
                        bot_id VARCHAR(64) NOT NULL,
                        bot_name VARCHAR(128) NOT NULL,
                        timestamp DATETIME,
                        event_type VARCHAR(32) NOT NULL,
                        side VARCHAR(8) NOT NULL,
                        quote_amount FLOAT DEFAULT 0.0,
                        price FLOAT DEFAULT 0.0,
                        trade_pnl FLOAT DEFAULT 0.0,
                        total_equity FLOAT DEFAULT 0.0,
                        trade_number INTEGER DEFAULT 0,
                        level_index INTEGER
                    )"""
                )
                conn.exec_driver_sql(
                    "CREATE INDEX idx_trade_events_bot_id ON trade_events(bot_id)"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX idx_trade_events_order_id ON trade_events(order_id)"
                )
                conn.commit()
        except Exception:
            pass


def _ensure_agent_uptime_column(engine: Engine) -> None:
    """Add ``uptime_seconds`` column to agents if missing."""
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(agents)").fetchall()
            columns = {row[1] for row in rows}
            if "uptime_seconds" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE agents ADD COLUMN uptime_seconds INTEGER DEFAULT 0"
                )
                conn.commit()
        except Exception:
            pass


def _add_trade_event_columns(engine: Engine) -> None:
    """Add evolving trade_events columns if missing."""
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(trade_events)").fetchall()
            columns = {row[1] for row in rows}
            if "order_id" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE trade_events ADD COLUMN order_id VARCHAR(128) DEFAULT NULL"
                )
                conn.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS idx_trade_events_order_id ON trade_events(order_id)"
                )
            if "market" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE trade_events ADD COLUMN market VARCHAR(32) DEFAULT ''"
                )
            if "linked_order_id" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE trade_events ADD COLUMN linked_order_id VARCHAR(64) DEFAULT NULL"
                )
            if "fee_paid_quote" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE trade_events ADD COLUMN fee_paid_quote FLOAT DEFAULT 0.0"
                )
            if "fee_rate" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE trade_events ADD COLUMN fee_rate FLOAT DEFAULT 0.0"
                )
            conn.commit()
        except Exception:
            pass
