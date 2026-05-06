"""Unit tests for manager.app.database – engine, session, and get_db."""
from __future__ import annotations

from manager.app.database import Base, SessionLocal, get_db


class TestGetDb:
    def test_yields_and_closes(self):
        gen = get_db()
        session = next(gen)
        assert session is not None
        # Exhaust the generator (triggers finally → close)
        try:
            next(gen)
        except StopIteration:
            pass

    def test_base_has_metadata(self):
        assert Base.metadata is not None

    def test_session_local_callable(self):
        session = SessionLocal()
        assert session is not None
        session.close()
