"""Database-backed settings store with grouped sections and exchange rows."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import json
import re
import uuid

from sqlalchemy.orm import Session

from manager.app.models import AppSetting

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MANAGER_ENV = _REPO_ROOT / "manager" / ".env"
_AGENT_ENV = _REPO_ROOT / "agent" / ".env"
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"

_ENV_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_EXCHANGE_KEY = "bitvavo"
_EXCHANGE_BASE_URL = "https://api.bitvavo.com/v2"
_EXCHANGE_WS_URL = "wss://ws.bitvavo.com/v2/"

_SCALAR_DESCRIPTIONS: dict[str, str] = {
    "COIN_MAP_SOURCE_URL": "Source URL used to refresh the coin map asset.",
    "COIN_MAP_SYNC_INTERVAL_SECONDS": "How often the manager refreshes the coin map asset.",
    "COIN_MAP_HTTP_TIMEOUT_SECONDS": "HTTP timeout used while downloading the coin map asset.",
    "MANAGER_BALANCE_CACHE_TTL_SECONDS": "How long the manager caches proxied balance responses.",
    "HEARTBEAT_TIMEOUT_SECONDS": "How long the manager waits before marking an agent offline.",
    "FAILOVER_INTERVAL_SECONDS": "How often the manager checks whether bots need failover.",
    "AGENT_BALANCE_CACHE_TTL_SECONDS": "How long the agent caches balance responses from the exchange.",
    "AGENT_TICKER_CACHE_TTL_SECONDS": "How long the agent caches ticker responses.",
    "MANAGER_INTERNAL_HTTP_URL": "Internal HTTP URL used for manager-to-manager callbacks.",
    "AGENT_HOST": "Preferred host name or IP address used by the agent for callbacks.",
    "AGENT_PORT": "Port exposed by the agent HTTP service.",
    "AGENT_BASE_URL": "Base callback URL the manager uses to reach the agent.",
    "AGENT_WS_TOKEN": "Shared token required for agent websocket connections.",
    "JWT_SECRET": "Secret used to sign manager session tokens.",
    "SESSION_MAX_HOURS": "Maximum session lifetime in hours.",
    "LIVE_EXCHANGE_PROVIDER": "Provider name used for live exchange connections.",
    "BITVAVO_DEFAULT_MARKET": "Default market used when no market is provided explicitly.",
    "SIM_MAKER_FEE_RATE": "Default maker fee rate used by the simulated exchange.",
    "SIM_FEE_RATE": "Fallback fee rate used by the simulated exchange.",
    "LIVE_FEE_RATE": "Default fee rate used by live bots when no per-bot override exists.",
}


def _parse_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key = match.group(1).strip()
        value = match.group(2).strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        out[key] = value
    return out


def _parse_compose_environment(path: Path) -> dict[str, dict[str, str]]:
    """Best-effort parser for docker-compose service environment blocks."""
    if not path.exists():
        return {}

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    in_services = False
    current_service = ""
    service_indent = 0
    in_env = False
    env_indent = 0
    out: dict[str, dict[str, str]] = {}

    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        stripped = raw.strip()

        if stripped == "services:":
            in_services = True
            current_service = ""
            in_env = False
            continue

        if not in_services:
            continue

        if indent == 2 and stripped.endswith(":") and not stripped.startswith("-"):
            current_service = stripped[:-1]
            service_indent = indent
            in_env = False
            out.setdefault(current_service, {})
            continue

        if current_service and indent <= service_indent and stripped.endswith(":"):
            current_service = ""
            in_env = False
            continue

        if not current_service:
            continue

        if stripped == "environment:":
            in_env = True
            env_indent = indent
            continue

        if in_env and indent <= env_indent:
            in_env = False

        if not in_env:
            continue

        if ":" in stripped and not stripped.startswith("-"):
            key, value = stripped.split(":", 1)
            key = key.strip()
            value = value.strip()
            if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            out[current_service][key] = value
            continue

        if stripped.startswith("-"):
            part = stripped[1:].strip()
            if "=" in part:
                key, value = part.split("=", 1)
                out[current_service][key.strip()] = value.strip()

    return out


def _humanize_key(key: str) -> str:
    parts = [part for part in str(key or "").strip().lower().replace("-", "_").split("_") if part]
    if not parts:
        return "Setting"
    return " ".join(part.capitalize() for part in parts)


def _source_for_key(key: str, default_source: str) -> str:
    upper = str(key or "").upper()
    if upper.startswith("AGENT_"):
        return "Agent"
    if upper.startswith("MANAGER_") or upper in {"HEARTBEAT_TIMEOUT_SECONDS", "FAILOVER_INTERVAL_SECONDS", "JWT_SECRET", "SESSION_MAX_HOURS", "INITIAL_ADMIN_USER", "INITIAL_ADMIN_PASS"}:
        return "Manager"
    if upper.startswith("COIN_MAP_") or upper.startswith("SIM_") or upper.startswith("LIVE_"):
        return "General"
    return default_source


def _setting_description(key: str, source: str) -> str:
    return _SCALAR_DESCRIPTIONS.get(str(key or "").upper(), f"{source} configuration for {_humanize_key(key).lower()}.")


def _first_non_empty(*values: str | None) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _collect_seed_sources() -> list[tuple[str, dict[str, str]]]:
    return [
        ("Manager", _parse_env_file(_MANAGER_ENV)),
        ("Agent", _parse_env_file(_AGENT_ENV)),
        ("Manager", _parse_compose_environment(_COMPOSE_FILE).get("manager", {})),
        ("Agent", _parse_compose_environment(_COMPOSE_FILE).get("agent1", {})),
    ]


def _iter_scalar_seed_rows() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for default_source, values in _collect_seed_sources():
        for key, value in values.items():
            if key in {"BITVAVO_API_KEY", "BITVAVO_API_SECRET", "BITVAVO_BASE_URL"}:
                continue
            source = _source_for_key(key, default_source)
            rows.append(
                {
                    "source": source,
                    "key": key,
                    "name": _humanize_key(key),
                    "description": _setting_description(key, source),
                    "value": str(value),
                }
            )
    return rows


def _iter_exchange_seed_rows() -> list[dict[str, str]]:
    seeded: list[dict[str, str]] = []
    sources = _collect_seed_sources()
    merged: dict[str, str] = {}
    for _, values in sources:
        merged.update(values)

    api_key = _first_non_empty(merged.get("BITVAVO_API_KEY"))
    api_secret = _first_non_empty(merged.get("BITVAVO_API_SECRET"))
    base_url = _first_non_empty(merged.get("BITVAVO_BASE_URL"), _EXCHANGE_BASE_URL)
    ws_url = _first_non_empty(merged.get("BITVAVO_WS_URL"), _EXCHANGE_WS_URL)
    default_market = _first_non_empty(merged.get("BITVAVO_DEFAULT_MARKET"), "BTC-EUR")
    provider = _first_non_empty(merged.get("LIVE_EXCHANGE_PROVIDER"), "bitvavo")

    if not api_key and not api_secret and not merged:
        return seeded

    seeded.append(
        {
            "source": "Exchange",
            "key": _EXCHANGE_KEY,
            "name": "Bitvavo",
            "description": "Bitvavo live exchange connection used by live bots.",
            "value": json.dumps(
                {
                    "provider": provider,
                    "base_url": base_url,
                    "ws_url": ws_url,
                    "endpoints_key": api_key,
                    "secret": api_secret,
                    "default_market": default_market,
                },
                ensure_ascii=True,
            ),
        }
    )
    return seeded


def _ensure_setting_row(db: Session, seed: dict[str, str]) -> None:
    source = str(seed.get("source", "General") or "General")
    key = str(seed.get("key", "") or "")
    if not key:
        return
    row = db.query(AppSetting).filter(AppSetting.source == source, AppSetting.key == key).first()
    now = datetime.now(UTC)
    if not row:
        row = AppSetting(
            source=source,
            key=key,
            name=str(seed.get("name", "") or _humanize_key(key)),
            description=str(seed.get("description", "") or _setting_description(key, source)),
            value=str(seed.get("value", "") or ""),
            updated_at=now,
        )
        db.add(row)
        return

    changed = False
    if not getattr(row, "name", ""):
        row.name = str(seed.get("name", "") or _humanize_key(key))
        changed = True
    if not getattr(row, "description", ""):
        row.description = str(seed.get("description", "") or _setting_description(key, source))
        changed = True
    if row.value is None:
        row.value = str(seed.get("value", "") or "")
        changed = True
    if changed:
        row.updated_at = now


def ensure_settings_seeded(db: Session) -> None:
    """Seed missing settings rows and backfill human-friendly labels."""
    seeds = _iter_scalar_seed_rows() + _iter_exchange_seed_rows()
    if not seeds:
        return
    for seed in seeds:
        _ensure_setting_row(db, seed)
    db.commit()


def list_settings(db: Session, source: str | None = None) -> list[AppSetting]:
    query = db.query(AppSetting)
    if source:
        query = query.filter(AppSetting.source == source)
    return query.order_by(AppSetting.source.asc(), AppSetting.name.asc(), AppSetting.key.asc()).all()


def list_exchanges(db: Session) -> list[AppSetting]:
    return list_settings(db, "Exchange")


def _serialize_setting(row: AppSetting) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "key": row.key,
        "name": row.name or _humanize_key(row.key),
        "description": row.description or _setting_description(row.key, row.source),
        "value": row.value,
        "updated_at": row.updated_at.isoformat().replace("+00:00", "Z") if row.updated_at else "",
    }


def build_settings_payload(db: Session) -> dict:
    rows = list_settings(db)
    sections: list[dict] = []
    grouped: dict[str, list[dict]] = {"General": [], "Agent": [], "Manager": [], "Exchange": []}
    for row in rows:
        grouped.setdefault(row.source, []).append(_serialize_setting(row))
    for source in ["General", "Agent", "Manager"]:
        sections.append({"source": source, "items": grouped.get(source, [])})
    exchanges = []
    for row in grouped.get("Exchange", []):
        try:
            config = json.loads(str(row.get("value", "") or "{}"))
        except Exception:
            config = {}
        exchanges.append(
            {
                **row,
                "base_url": str(config.get("base_url", "") or ""),
                "ws_url": str(config.get("ws_url", "") or ""),
                "endpoints_key": str(config.get("endpoints_key", "") or ""),
                "secret": str(config.get("secret", "") or ""),
                "provider": str(config.get("provider", "") or "bitvavo"),
                "default_market": str(config.get("default_market", "") or ""),
            }
        )
    return {"sections": sections, "exchanges": exchanges}


def build_runtime_snapshot(db: Session) -> dict:
    rows = list_settings(db)
    sources: dict[str, dict[str, str]] = {"General": {}, "Agent": {}, "Manager": {}}
    flat: dict[str, str] = {}
    exchanges: list[dict] = []

    for row in rows:
        serialized = _serialize_setting(row)
        if row.source == "Exchange":
            try:
                config = json.loads(str(row.value or "{}"))
            except Exception:
                config = {}
            exchanges.append(
                {
                    "id": row.id,
                    "key": row.key,
                    "name": row.name or _humanize_key(row.key),
                    "description": row.description or _setting_description(row.key, row.source),
                    "provider": str(config.get("provider", "") or "bitvavo"),
                    "base_url": str(config.get("base_url", "") or ""),
                    "ws_url": str(config.get("ws_url", "") or ""),
                    "endpoints_key": str(config.get("endpoints_key", "") or ""),
                    "secret": str(config.get("secret", "") or ""),
                    "default_market": str(config.get("default_market", "") or ""),
                    "updated_at": serialized["updated_at"],
                }
            )
            continue
        sources.setdefault(row.source, {})[row.key] = str(row.value or "")
        flat[row.key] = str(row.value or "")

    return {"sources": sources, "flat": flat, "exchanges": exchanges}


def update_settings(db: Session, updates: list[dict]) -> list[int]:
    now = datetime.now(UTC)
    saved: list[int] = []
    for item in updates:
        try:
            sid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        row = db.query(AppSetting).filter(AppSetting.id == sid).first()
        if not row or row.source == "Exchange":
            continue
        value = str(item.get("value", row.value or ""))
        row.value = value
        row.updated_at = now
        saved.append(sid)

    if saved:
        db.commit()
    return saved


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return slug or f"exchange-{uuid.uuid4().hex[:8]}"


def _exchange_value_from_payload(payload: dict) -> str:
    return json.dumps(
        {
            "provider": str(payload.get("provider", "bitvavo") or "bitvavo"),
            "base_url": str(payload.get("base_url", "") or "").strip(),
            "ws_url": str(payload.get("ws_url", "") or "").strip(),
            "endpoints_key": str(payload.get("endpoints_key", "") or "").strip(),
            "secret": str(payload.get("secret", "") or "").strip(),
            "default_market": str(payload.get("default_market", "") or "").strip().upper(),
        },
        ensure_ascii=True,
    )


def create_exchange(db: Session, payload: dict) -> dict:
    name = str(payload.get("name", "") or "").strip()
    if not name:
        raise ValueError("Exchange name is required")
    base_url = str(payload.get("base_url", "") or "").strip()
    endpoints_key = str(payload.get("endpoints_key", "") or "").strip()
    secret = str(payload.get("secret", "") or "").strip()
    if not base_url or not endpoints_key or not secret:
        raise ValueError("Exchange base_url, endpoints_key and secret are required")

    now = datetime.now(UTC)
    row = AppSetting(
        source="Exchange",
        key=_slugify(name),
        name=name,
        description=str(payload.get("description", "") or "").strip(),
        value=_exchange_value_from_payload(payload),
        updated_at=now,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "source": row.source,
        "key": row.key,
        "name": row.name,
        "description": row.description,
        **json.loads(row.value or "{}"),
        "updated_at": row.updated_at.isoformat().replace("+00:00", "Z") if row.updated_at else "",
    }


def update_exchange(db: Session, exchange_id: int, payload: dict) -> dict:
    row = db.query(AppSetting).filter(AppSetting.id == int(exchange_id), AppSetting.source == "Exchange").first()
    if not row:
        raise ValueError("Exchange not found")
    now = datetime.now(UTC)
    if "name" in payload:
        name = str(payload.get("name", "") or "").strip()
        if name:
            row.name = name
    if "description" in payload:
        row.description = str(payload.get("description", "") or "").strip()

    try:
        current = json.loads(row.value or "{}")
    except Exception:
        current = {}
    for field in ["provider", "base_url", "ws_url", "endpoints_key", "secret", "default_market"]:
        if field in payload:
            current[field] = str(payload.get(field, "") or "").strip()
    if "default_market" in current:
        current["default_market"] = str(current["default_market"] or "").strip().upper()

    row.value = json.dumps(current, ensure_ascii=True)
    row.updated_at = now
    db.commit()
    db.refresh(row)
    return {
        "id": row.id,
        "source": row.source,
        "key": row.key,
        "name": row.name,
        "description": row.description,
        **current,
        "updated_at": row.updated_at.isoformat().replace("+00:00", "Z") if row.updated_at else "",
    }


def delete_exchange(db: Session, exchange_id: int) -> None:
    row = db.query(AppSetting).filter(AppSetting.id == int(exchange_id), AppSetting.source == "Exchange").first()
    if not row:
        raise ValueError("Exchange not found")
    db.delete(row)
    db.commit()
