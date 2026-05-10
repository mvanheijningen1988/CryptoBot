"""Structured diagnostics logging with correlation context and 48h retention."""
from __future__ import annotations

import json
import logging
import os
import threading
import traceback
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

_CORRELATION_ID: ContextVar[str | None] = ContextVar("correlation_id", default=None)
_REQUEST_ID: ContextVar[str | None] = ContextVar("request_id", default=None)
_AGENT_ID: ContextVar[str | None] = ContextVar("agent_id", default=None)
_BOT_ID: ContextVar[str | None] = ContextVar("bot_id", default=None)
_COMPONENT: ContextVar[str | None] = ContextVar("component", default=None)


@dataclass
class _ContextSnapshot:
    correlation_id: str | None
    request_id: str | None
    agent_id: str | None
    bot_id: str | None
    component: str | None


class _LevelFilter(logging.Filter):
    """Only allow records matching one diagnostics kind."""

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind

    def filter(self, record: logging.LogRecord) -> bool:
        record_kind = str(getattr(record, "diag_kind", "debug") or "debug").lower()
        if record_kind not in {"debug", "trace"}:
            record_kind = "debug"
        return record_kind == self.kind


class _HourlyJsonHandler(logging.Handler):
    """Write JSON lines to one file per hour with retention cleanup."""

    def __init__(self, service_name: str, logs_root: Path, kind: str) -> None:
        super().__init__(level=logging.DEBUG)
        self.service_name = service_name
        self.logs_root = logs_root
        self.kind = kind
        self.instance_id = os.getenv("HOSTNAME", "local")
        self._lock = threading.Lock()
        self._last_cleanup_hour = ""

    def _target_path(self, now: datetime) -> Path:
        ts_hour = now.strftime("%Y%m%d%H")
        folder = self.logs_root / self.service_name / self.kind
        folder.mkdir(parents=True, exist_ok=True)
        return folder / f"{self.service_name}_{self.kind}_{ts_hour}.log"

    def _cleanup_old_files(self, now: datetime, retention_hours: int) -> None:
        current_hour = now.strftime("%Y%m%d%H")
        if self._last_cleanup_hour == current_hour:
            return
        self._last_cleanup_hour = current_hour

        cutoff = now - timedelta(hours=retention_hours)
        root = self.logs_root
        if not root.exists():
            return

        for path in root.rglob("*.log"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError:
                continue
            if mtime < cutoff:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            now = datetime.now(UTC)
            retention_hours = max(1, int(os.getenv("DIAGNOSTICS_RETENTION_HOURS", "48") or 48))
            line = {
                "timestamp": now.isoformat(),
                "service": self.service_name,
                "instance_id": self.instance_id,
                "level": record.levelname,
                "kind": str(getattr(record, "diag_kind", "debug") or "debug").lower(),
                "event": str(getattr(record, "diag_event", record.name) or record.name),
                "logger": record.name,
                "message": record.getMessage(),
                "correlation_id": str(getattr(record, "correlation_id", "") or ""),
                "request_id": str(getattr(record, "request_id", "") or ""),
                "agent_id": str(getattr(record, "agent_id", "") or ""),
                "bot_id": str(getattr(record, "bot_id", "") or ""),
                "component": str(getattr(record, "component", "") or record.name),
                "fields": getattr(record, "diag_fields", {}) or {},
            }
            if record.exc_info:
                line["exception"] = "".join(traceback.format_exception(*record.exc_info))

            target = self._target_path(now)
            with self._lock:
                self._cleanup_old_files(now, retention_hours)
                with target.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(line, ensure_ascii=True) + "\n")
        except Exception:
            self.handleError(record)


class _ContextEnricher(logging.Filter):
    """Inject current correlation context into every log record."""

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self.service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "correlation_id"):
            record.correlation_id = _CORRELATION_ID.get() or ""
        if not hasattr(record, "request_id"):
            record.request_id = _REQUEST_ID.get() or ""
        if not hasattr(record, "agent_id"):
            record.agent_id = _AGENT_ID.get() or ""
        if not hasattr(record, "bot_id"):
            record.bot_id = _BOT_ID.get() or ""
        if not hasattr(record, "component"):
            record.component = _COMPONENT.get() or record.name
        if not hasattr(record, "diag_kind"):
            record.diag_kind = "debug"
        if not hasattr(record, "diag_event"):
            record.diag_event = record.name
        if not hasattr(record, "diag_fields"):
            record.diag_fields = {}
        return True


_config_lock = threading.Lock()
_configured_services: set[str] = set()


def _resolve_log_root() -> Path:
    """Resolve a writable diagnostics root with safe fallback for tests."""
    preferred = Path(os.getenv("DIAGNOSTICS_LOG_DIR", "/app/data/diagnostics"))
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = Path.cwd() / "data" / "diagnostics"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def configure_diagnostics_logging(service_name: str) -> Path:
    """Configure root logging handlers for debug + trace JSON files."""
    with _config_lock:
        if service_name in _configured_services:
            logs_root = _resolve_log_root()
            return logs_root

        logs_root = _resolve_log_root()

        root = logging.getLogger()
        root.setLevel(logging.DEBUG)

        # Keep one console stream handler for runtime visibility.
        if not any(isinstance(h, logging.StreamHandler) and getattr(h, "_cryptobot_console", False) for h in root.handlers):
            console = logging.StreamHandler()
            console.setLevel(logging.INFO)
            console._cryptobot_console = True  # type: ignore[attr-defined]
            console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            console.addFilter(_ContextEnricher(service_name))
            root.addHandler(console)

        debug_handler = _HourlyJsonHandler(service_name=service_name, logs_root=logs_root, kind="debug")
        debug_handler.addFilter(_ContextEnricher(service_name))
        debug_handler.addFilter(_LevelFilter("debug"))
        root.addHandler(debug_handler)

        trace_handler = _HourlyJsonHandler(service_name=service_name, logs_root=logs_root, kind="trace")
        trace_handler.addFilter(_ContextEnricher(service_name))
        trace_handler.addFilter(_LevelFilter("trace"))
        root.addHandler(trace_handler)

        _configured_services.add(service_name)
        return logs_root


def get_correlation_id() -> str:
    """Return current correlation id or create one."""
    cid = _CORRELATION_ID.get()
    if cid:
        return cid
    cid = str(uuid4())
    _CORRELATION_ID.set(cid)
    return cid


def get_diagnostics_log_root() -> Path:
    """Return diagnostics root folder path."""
    return _resolve_log_root()


def set_context(
    *,
    correlation_id: str | None = None,
    request_id: str | None = None,
    agent_id: str | None = None,
    bot_id: str | None = None,
    component: str | None = None,
) -> _ContextSnapshot:
    """Set diagnostics context vars, returning previous values."""
    previous = _ContextSnapshot(
        correlation_id=_CORRELATION_ID.get(),
        request_id=_REQUEST_ID.get(),
        agent_id=_AGENT_ID.get(),
        bot_id=_BOT_ID.get(),
        component=_COMPONENT.get(),
    )
    if correlation_id is not None:
        _CORRELATION_ID.set(correlation_id)
    if request_id is not None:
        _REQUEST_ID.set(request_id)
    if agent_id is not None:
        _AGENT_ID.set(agent_id)
    if bot_id is not None:
        _BOT_ID.set(bot_id)
    if component is not None:
        _COMPONENT.set(component)
    return previous


def restore_context(previous: _ContextSnapshot) -> None:
    """Restore a previously captured diagnostics context."""
    _CORRELATION_ID.set(previous.correlation_id)
    _REQUEST_ID.set(previous.request_id)
    _AGENT_ID.set(previous.agent_id)
    _BOT_ID.set(previous.bot_id)
    _COMPONENT.set(previous.component)


@contextmanager
def scoped_context(**kwargs: str | None) -> Iterator[None]:
    """Temporarily override diagnostics context for one block."""
    previous = set_context(**kwargs)
    try:
        yield
    finally:
        restore_context(previous)


def trace_log(logger: logging.Logger, event: str, message: str, **fields: Any) -> None:
    """Emit one trace-level diagnostics entry."""
    trace_fields = dict(fields)
    payload_value = trace_fields.get("payload")
    if not isinstance(payload_value, dict):
        trace_fields["payload"] = dict(trace_fields)

    logger.debug(
        message,
        extra={
            "diag_kind": "trace",
            "diag_event": event,
            "diag_fields": trace_fields,
        },
    )


def debug_log(logger: logging.Logger, event: str, message: str, **fields: Any) -> None:
    """Emit one debug diagnostics entry."""
    logger.debug(
        message,
        extra={
            "diag_kind": "debug",
            "diag_event": event,
            "diag_fields": fields,
        },
    )
