"""Diagnostics endpoints for debug log browsing and downloads."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, HTTPException
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRouter
from sqlalchemy.orm import Session

from common.diagnostics import get_diagnostics_log_root
from manager.app.database import get_db
from manager.app.models import Agent, Bot

router = APIRouter(prefix="/debug", tags=["debug"])

DbSession = Annotated[Session, Depends(get_db)]


def _parse_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _classify_instance(entry: dict[str, Any]) -> tuple[str, str]:
    bot_id = str(entry.get("bot_id") or "")
    if bot_id:
        return "bot", bot_id
    service = str(entry.get("service") or "")
    if service == "agent":
        aid = str(entry.get("agent_id") or entry.get("instance_id") or "")
        return "agent", aid
    return "manager", str(entry.get("instance_id") or "local")


def _iter_entries(kind: str = "debug") -> list[dict[str, Any]]:
    logs_root = get_diagnostics_log_root()
    if not logs_root.exists():
        return []

    retention_hours = 48
    cutoff = datetime.now(UTC) - timedelta(hours=retention_hours)
    paths = sorted(logs_root.glob(f"*/{kind}/*.log"), reverse=True)
    entries: list[dict[str, Any]] = []

    for path in paths:
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        item = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    ts = _parse_ts(item.get("timestamp"))
                    if ts and ts < cutoff:
                        continue
                    item["instance_type"], item["instance_id_resolved"] = _classify_instance(item)
                    item["_sort_ts"] = ts.isoformat() if ts else ""
                    entries.append(item)
        except OSError:
            continue

    entries.sort(key=lambda row: row.get("_sort_ts", ""), reverse=True)
    return entries


def _filter_entries(
    rows: list[dict[str, Any]],
    *,
    instance_type: str | None = None,
    instance_id: str | None = None,
    service: str | None = None,
    component: str | None = None,
    level: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if instance_type and row.get("instance_type") != instance_type:
            continue
        if instance_id and row.get("instance_id_resolved") != instance_id:
            continue
        if service and str(row.get("service") or "") != service:
            continue
        if component and component not in str(row.get("component") or ""):
            continue
        if level and str(row.get("level") or "").upper() != level.upper():
            continue
        out.append(row)
    return out


@router.get("/logs")
def get_debug_logs(
    kind: str = "debug",
    instance_type: str | None = None,
    instance_id: str | None = None,
    service: str | None = None,
    component: str | None = None,
    level: str | None = None,
    limit: int = 500,
) -> dict:
    """Return diagnostics logs (debug by default) for dashboard diagnostics tab."""
    safe_kind = str(kind or "debug").lower()
    if safe_kind not in {"debug", "trace"}:
        raise HTTPException(status_code=400, detail="kind must be debug or trace")

    rows = _iter_entries(safe_kind)
    rows = _filter_entries(
        rows,
        instance_type=instance_type,
        instance_id=instance_id,
        service=service,
        component=component,
        level=level,
    )
    safe_limit = max(1, min(limit, 5000))

    clean_rows = []
    for row in rows[:safe_limit]:
        row = dict(row)
        row.pop("_sort_ts", None)
        clean_rows.append(row)

    return {"kind": safe_kind, "count": len(clean_rows), "logs": clean_rows}


@router.get("/logs/download")
def download_debug_logs(
    kind: str = "debug",
    instance_type: str | None = None,
    instance_id: str | None = None,
    service: str | None = None,
    component: str | None = None,
    level: str | None = None,
    limit: int = 2000,
) -> PlainTextResponse:
    """Download filtered diagnostics logs as NDJSON text file."""
    payload = get_debug_logs(
        kind=kind,
        instance_type=instance_type,
        instance_id=instance_id,
        service=service,
        component=component,
        level=level,
        limit=limit,
    )
    lines = [json.dumps(row, ensure_ascii=True) for row in payload["logs"]]
    body = "\n".join(lines) + ("\n" if lines else "")
    filename = f"cryptobot_{payload['kind']}_logs.ndjson"
    return PlainTextResponse(
        body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/instances")
def list_instances(db: DbSession) -> dict:
    """Return diagnostics instance overview including historical (log-derived) entries."""
    rows = _iter_entries("debug")

    # Collect historical instances from logs.
    historical: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        inst_type = str(row.get("instance_type") or "")
        inst_id = str(row.get("instance_id_resolved") or "")
        if not inst_type or not inst_id:
            continue
        ts = str(row.get("timestamp") or "")
        key = (inst_type, inst_id)
        item = historical.get(key)
        if item is None:
            historical[key] = {
                "instance_type": inst_type,
                "instance_id": inst_id,
                "status": "historical",
                "first_seen": ts,
                "last_seen": ts,
                "source": "logs",
            }
            continue
        if ts and (not item["first_seen"] or ts < item["first_seen"]):
            item["first_seen"] = ts
        if ts and (not item["last_seen"] or ts > item["last_seen"]):
            item["last_seen"] = ts

    # Current manager instance from logs.
    for key, item in list(historical.items()):
        if item["instance_type"] == "manager":
            item["status"] = "active"
            item["source"] = "runtime+logs"

    # Overlay current DB agents and bots.
    for agent in db.query(Agent).all():
        key = ("agent", agent.id)
        row = historical.get(key, {
            "instance_type": "agent",
            "instance_id": agent.id,
            "first_seen": "",
            "last_seen": "",
            "source": "runtime",
        })
        row["status"] = str(agent.status)
        row["source"] = "runtime+logs" if key in historical else "runtime"
        heartbeat = agent.last_heartbeat.isoformat() + "Z" if agent.last_heartbeat else ""
        if heartbeat:
            row["last_seen"] = max(str(row.get("last_seen") or ""), heartbeat)
            if not row.get("first_seen"):
                row["first_seen"] = heartbeat
        historical[key] = row

    for bot in db.query(Bot).all():
        key = ("bot", bot.id)
        row = historical.get(key, {
            "instance_type": "bot",
            "instance_id": bot.id,
            "first_seen": "",
            "last_seen": "",
            "source": "runtime",
        })
        row["status"] = str(bot.status)
        row["source"] = "runtime+logs" if key in historical else "runtime"
        updated = bot.updated_at.isoformat() + "Z" if bot.updated_at else ""
        if updated:
            row["last_seen"] = max(str(row.get("last_seen") or ""), updated)
            if not row.get("first_seen"):
                row["first_seen"] = updated
        historical[key] = row

    instances = sorted(
        historical.values(),
        key=lambda item: (str(item.get("instance_type") or ""), str(item.get("instance_id") or "")),
    )

    return {
        "retention_hours": 48,
        "instances": instances,
    }
