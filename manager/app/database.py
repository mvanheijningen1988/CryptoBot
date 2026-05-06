"""Database engine and session factory for the CryptoBot manager.

Reads ``DB_URL`` from the environment (defaults to a local SQLite file)
and exposes a ``get_db()`` FastAPI dependency for request-scoped sessions.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DB_URL = os.getenv("DB_URL", "sqlite:///./data/manager.db")

connect_args = {"check_same_thread": False} if DB_URL.startswith("sqlite") else {}
engine = create_engine(DB_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    """
    Yield a SQLAlchemy session and ensure it is closed after the request.

    :return: A generator yielding a SQLAlchemy Session instance.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
