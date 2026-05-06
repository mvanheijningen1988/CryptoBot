"""Unit tests for manager.app.auth – password hashing, JWT, and admin bootstrap."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import jwt
import pytest
from fastapi import HTTPException

from manager.app.auth import (
    JWT_ALGORITHM,
    JWT_SECRET,
    create_token,
    decode_token,
    _extract_token,
    ensure_admin_user,
    hash_password,
    verify_password,
)
from manager.app.models import User


# ── Password hashing ────────────────────────────────────────────────


class TestHashPassword:
    def test_returns_bcrypt_string(self):
        h = hash_password("hello")
        assert h.startswith("$2b$")

    def test_different_calls_produce_different_hashes(self):
        a = hash_password("same")
        b = hash_password("same")
        assert a != b  # salted

    def test_empty_password(self):
        h = hash_password("")
        assert h.startswith("$2b$")

    def test_unicode_password(self):
        h = hash_password("wàchtw00rd-日本語")
        assert verify_password("wàchtw00rd-日本語", h)

    def test_very_long_password(self):
        # bcrypt truncates at 72 bytes; hashing should still work
        long_pw = "A" * 200
        h = hash_password(long_pw)
        assert h.startswith("$2b$")


class TestVerifyPassword:
    def test_correct_password(self):
        h = hash_password("correct")
        assert verify_password("correct", h) is True

    def test_wrong_password(self):
        h = hash_password("correct")
        assert verify_password("wrong", h) is False

    def test_empty_vs_nonempty(self):
        h = hash_password("notempty")
        assert verify_password("", h) is False

    def test_empty_vs_empty(self):
        h = hash_password("")
        assert verify_password("", h) is True

    def test_case_sensitive(self):
        h = hash_password("Password")
        assert verify_password("password", h) is False


# ── JWT tokens ───────────────────────────────────────────────────────


class TestCreateToken:
    def test_returns_string(self):
        token = create_token("user-1", "admin")
        assert isinstance(token, str) and len(token) > 20

    def test_payload_contains_sub_and_role(self):
        token = create_token("uid-42", "viewer")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        assert payload["sub"] == "uid-42"
        assert payload["role"] == "viewer"

    def test_token_has_future_expiry(self):
        token = create_token("u", "admin")
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
        assert exp > datetime.now(timezone.utc)


class TestDecodeToken:
    def test_valid_token(self):
        token = create_token("u1", "admin")
        payload = decode_token(token)
        assert payload["sub"] == "u1"
        assert payload["role"] == "admin"

    def test_expired_token_raises_401(self):
        expired_payload = {
            "sub": "u1",
            "role": "admin",
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        }
        token = jwt.encode(expired_payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401
        assert "expired" in exc_info.value.detail.lower()

    def test_invalid_signature_raises_401(self):
        token = jwt.encode({"sub": "u", "role": "a"}, "wrong-secret", algorithm=JWT_ALGORITHM)
        with pytest.raises(HTTPException) as exc_info:
            decode_token(token)
        assert exc_info.value.status_code == 401
        assert "invalid" in exc_info.value.detail.lower()

    def test_garbage_token_raises_401(self):
        with pytest.raises(HTTPException) as exc_info:
            decode_token("not.a.jwt")
        assert exc_info.value.status_code == 401

    def test_empty_token_raises_401(self):
        with pytest.raises(HTTPException):
            decode_token("")


# ── _extract_token ───────────────────────────────────────────────────


class TestExtractToken:
    def _make_request(self, *, headers=None, cookies=None):
        """Build a minimal mock request."""
        from unittest.mock import MagicMock

        req = MagicMock()
        req.headers = headers or {}
        req.cookies = cookies or {}
        return req

    def test_bearer_header(self):
        req = self._make_request(headers={"Authorization": "Bearer abc123"})
        assert _extract_token(req) == "abc123"

    def test_cookie_fallback(self):
        req = self._make_request(cookies={"token": "cookie-tok"})
        assert _extract_token(req) == "cookie-tok"

    def test_bearer_preferred_over_cookie(self):
        req = self._make_request(
            headers={"Authorization": "Bearer from-header"},
            cookies={"token": "from-cookie"},
        )
        assert _extract_token(req) == "from-header"

    def test_no_auth_raises_401(self):
        req = self._make_request()
        with pytest.raises(HTTPException) as exc_info:
            _extract_token(req)
        assert exc_info.value.status_code == 401

    def test_non_bearer_auth_header_falls_through(self):
        req = self._make_request(headers={"Authorization": "Basic abc"})
        with pytest.raises(HTTPException):
            _extract_token(req)


# ── ensure_admin_user ────────────────────────────────────────────────


class TestEnsureAdminUser:
    def test_creates_admin_when_missing(self, db_session):
        with patch.dict(os.environ, {"ADMIN_USER": "boss", "ADMIN_PASSWORD": "secret123"}):
            ensure_admin_user(db_session)
        user = db_session.query(User).filter(User.username == "boss").first()
        assert user is not None
        assert user.role == "admin"
        assert user.must_change_password is True
        assert verify_password("secret123", user.password_hash)

    def test_skips_when_no_password(self, db_session):
        with patch.dict(os.environ, {"ADMIN_USER": "admin", "ADMIN_PASSWORD": ""}):
            ensure_admin_user(db_session)
        assert db_session.query(User).count() == 0

    def test_resyncs_changed_password(self, db_session):
        # Create admin with old password
        user = User(
            id=str(uuid.uuid4()),
            username="admin",
            password_hash=hash_password("old-password"),
            role="admin",
            locale="en",
            must_change_password=False,
        )
        db_session.add(user)
        db_session.commit()

        # Now env has a different password
        with patch.dict(os.environ, {"ADMIN_USER": "admin", "ADMIN_PASSWORD": "new-password"}):
            ensure_admin_user(db_session)

        db_session.refresh(user)
        assert verify_password("new-password", user.password_hash)
        assert user.must_change_password is True

    def test_no_resync_when_password_matches(self, db_session):
        user = User(
            id=str(uuid.uuid4()),
            username="admin",
            password_hash=hash_password("same-pass"),
            role="admin",
            locale="en",
            must_change_password=False,
        )
        db_session.add(user)
        db_session.commit()

        with patch.dict(os.environ, {"ADMIN_USER": "admin", "ADMIN_PASSWORD": "same-pass"}):
            ensure_admin_user(db_session)

        db_session.refresh(user)
        # must_change_password should stay False – no resync needed
        assert user.must_change_password is False
