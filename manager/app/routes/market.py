"""Market data proxy endpoints: balance, 24h summary, and market listing."""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import threading
import time as _time
import uuid
from datetime import UTC, datetime
from typing import Annotated

import requests
import websocket
from fastapi import Depends
from fastapi import HTTPException
from fastapi.routing import APIRouter
from sqlalchemy import func
from sqlalchemy.orm import Session

from manager.app.database import get_db
from manager.app.models import Agent, Bot, TradeEvent
from manager.app.services.runtime_settings import get_exchange, get_float

router = APIRouter()
DbSession = Annotated[Session, Depends(get_db)]

_BALANCE_PROXY_CACHE_LOCK = threading.Lock()
_BALANCE_PROXY_CACHE: dict[str, object] = {"ts": 0.0, "rows": []}


def _bitvavo_auth_settings() -> tuple[str, str, str, str]:
    """Resolve Bitvavo auth config, honoring explicit env overrides first."""
    exchange = get_exchange("bitvavo")

    # Tests and operators can force credentials via env vars; explicit blanks
    # should be treated as intentionally missing.
    env_key_set = "BITVAVO_API_KEY" in os.environ
    env_secret_set = "BITVAVO_API_SECRET" in os.environ

    if env_key_set or env_secret_set:
        api_key = str(os.getenv("BITVAVO_API_KEY", "") or "").strip()
        api_secret = str(os.getenv("BITVAVO_API_SECRET", "") or "").strip()
    else:
        api_key = str(exchange.get("endpoints_key", "") or "").strip()
        api_secret = str(exchange.get("secret", "") or "").strip()

    base_url = str(os.getenv("BITVAVO_BASE_URL", "") or "").strip() or str(exchange.get("base_url", "") or "").strip() or "https://api.bitvavo.com/v2"
    ws_url = str(os.getenv("BITVAVO_WS_URL", "") or "").strip() or str(exchange.get("ws_url", "") or "").strip() or "wss://ws.bitvavo.com/v2/"
    return api_key, api_secret, base_url, ws_url


def _get_cached_proxy_balance_rows(now: float) -> list[dict] | None:
    with _BALANCE_PROXY_CACHE_LOCK:
        ts = float(_BALANCE_PROXY_CACHE.get("ts", 0.0) or 0.0)
        rows = _BALANCE_PROXY_CACHE.get("rows")
        if now - ts <= max(0.0, float(get_float("MANAGER_BALANCE_CACHE_TTL_SECONDS", 15.0))) and isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return None


def _get_stale_proxy_balance_rows() -> list[dict] | None:
    with _BALANCE_PROXY_CACHE_LOCK:
        rows = _BALANCE_PROXY_CACHE.get("rows")
        if isinstance(rows, list) and rows:
            return [dict(row) for row in rows if isinstance(row, dict)]
    return None


def _set_proxy_balance_rows(now: float, rows: list[dict]) -> None:
    with _BALANCE_PROXY_CACHE_LOCK:
        _BALANCE_PROXY_CACHE["ts"] = now
        _BALANCE_PROXY_CACHE["rows"] = [dict(row) for row in rows if isinstance(row, dict)]


def _bitvavo_private_ws_call(action: str, payload: dict | None = None, timeout_seconds: float = 8.0) -> dict:
    """Execute a private Bitvavo websocket action and return its response object.

    The call authenticates on a short-lived websocket connection and waits for
    the response matching a generated ``requestId``.
    """
    api_key, api_secret, _, ws_url = _bitvavo_auth_settings()
    if not api_key or not api_secret:
        raise RuntimeError("Bitvavo API credentials not configured")

    ws = None
    req_id = int(_time.time() * 1000)
    auth_req_id = req_id - 1
    deadline = _time.time() + timeout_seconds
    body = payload or {}

    try:
        ws = websocket.create_connection(ws_url, timeout=timeout_seconds)
        timestamp_ms = int(_time.time() * 1000)
        signature = hmac.new(
            api_secret.encode("utf-8"),
            f"{timestamp_ms}GET/v2/websocket".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        ws.send(
            json.dumps(
                {
                    "action": "authenticate",
                    "requestId": auth_req_id,
                    "key": api_key,
                    "signature": signature,
                    "timestamp": timestamp_ms,
                }
            )
        )

        # Wait for authentication confirmation before private calls.
        while _time.time() < deadline:
            raw = ws.recv()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("requestId") != auth_req_id:
                continue
            if msg.get("errorCode") is not None:
                raise RuntimeError(f"Bitvavo websocket error {msg.get('errorCode')}: {msg.get('error')}")
            break
        else:
            raise TimeoutError("Timed out waiting for Bitvavo authentication")

        request = {"action": action, "requestId": req_id, **body}
        ws.send(json.dumps(request))

        while _time.time() < deadline:
            raw = ws.recv()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if not isinstance(msg, dict):
                continue
            if msg.get("requestId") != req_id:
                continue
            if msg.get("errorCode") is not None:
                raise RuntimeError(f"Bitvavo websocket error {msg.get('errorCode')}: {msg.get('error')}")
            return msg.get("response", {}) if isinstance(msg.get("response"), dict) else {}

        raise TimeoutError(f"Timed out waiting for Bitvavo response to {action}")
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


def _to_float(value: object) -> float:
    """Best-effort float parser returning 0.0 on invalid input."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _bitvavo_signed_get(url_path: str, timeout: float = 6.0) -> requests.Response:
    """Execute a signed Bitvavo REST GET request for private endpoints."""
    api_key, api_secret, base_url, _ = _bitvavo_auth_settings()
    if not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Bitvavo API credentials not configured")

    timestamp = str(int(_time.time() * 1000))
    method = "GET"
    signature = hmac.new(
        api_secret.encode("utf-8"),
        f"{timestamp}{method}/v2{url_path}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "BITVAVO-ACCESS-KEY": api_key,
        "BITVAVO-ACCESS-SIGNATURE": signature,
        "BITVAVO-ACCESS-TIMESTAMP": timestamp,
    }
    return requests.get(f"{base_url}{url_path}", headers=headers, timeout=timeout)


def _fmt_order_type(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw == "market":
        return "Market"
    if raw == "stoploss":
        return "StopLoss"
    if raw == "stoplosslimit":
        return "StopLossLimit"
    return "Limit"


def _ws_list(action: str, payload: dict | None = None) -> list[dict]:
    """Execute a private websocket action and return a list response."""
    response = _bitvavo_private_ws_call(action, payload or {})
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        for key in ("items", "orders", "trades", "balances"):
            data = response.get(key)
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
    return []


def _list_online_agents(db: Session) -> list[Agent]:
    return (
        db.query(Agent)
        .filter(Agent.status == "online", Agent.approval_status == "approved")
        .all()
    )


def _proxy_notifications_rows(db: Session, path: str, params: dict[str, str] | None = None) -> list[dict]:
    """Fetch notification rows from available online agents and merge unique rows."""
    agents = _list_online_agents(db)
    if not agents:
        return []

    merged: list[dict] = []
    seen: set[str] = set()

    for agent in agents:
        try:
            resp = requests.get(f"{agent.base_url}{path}", params=params or None, timeout=6)
            resp.raise_for_status()
            payload = resp.json()
            rows = payload.get("rows") if isinstance(payload, dict) else []
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_id = str(row.get("id", "") or row.get("order_id", "") or "")
                row_key = row_id or json.dumps(row, sort_keys=True, separators=(",", ":"))
                if row_key in seen:
                    continue
                seen.add(row_key)
                merged.append(row)
        except Exception:
            continue

    return merged


def _is_open_order_status(status: str) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized in {"new", "awaitingtrigger", "partiallyfilled"}


def _event_status(event_type: str) -> str:
    normalized = str(event_type or "").strip().lower()
    if normalized == "order_cancelled":
        return "cancelled"
    if normalized == "order_filled":
        return "filled"
    return "new"


def _parse_market_list(markets: str | None) -> list[str]:
    return [m.strip().upper() for m in str(markets or "").split(",") if m and m.strip()]


def _base_quote_from_market(market: str) -> tuple[str, str]:
    if "-" in market:
        base, quote = market.split("-", 1)
        return base, quote
    return "", "EUR"


def _event_iso(ts: datetime | None) -> str:
    if ts is None:
        return ""
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _registered_bot_ids(db: Session) -> set[str]:
    rows = db.query(Bot.id).all()
    return {str(row[0]) for row in rows if row and row[0] is not None}


def _bitvavo_operator_id_from_bot_id(bot_id: str) -> int:
    """Derive the same positive int64 operatorId logic used by the runner."""
    try:
        bot_uuid = uuid.UUID(str(bot_id))
        return bot_uuid.int % ((1 << 63) - 1)
    except ValueError:
        digest = hashlib.sha256(str(bot_id).encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big") & ((1 << 63) - 1)


def _registered_operator_ids(db: Session) -> set[int]:
    return {_bitvavo_operator_id_from_bot_id(bot_id) for bot_id in _registered_bot_ids(db)}


def _extract_row_operator_id(row: dict) -> int | None:
    raw = row.get("operator_id")
    if raw is None:
        raw = row.get("operatorId")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _filter_rows_by_known_operator_ids(rows: list[dict], known_operator_ids: set[int]) -> list[dict]:
    if not known_operator_ids:
        return []
    filtered: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        operator_id = _extract_row_operator_id(row)
        if operator_id is None:
            continue
        if operator_id not in known_operator_ids:
            continue
        filtered.append(row)
    return filtered


def _known_order_keys_for_registered_bots(db: Session, markets: str | None = None) -> set[str]:
    bot_ids = _registered_bot_ids(db)
    if not bot_ids:
        return set()

    parsed_markets = _parse_market_list(markets)
    q = db.query(TradeEvent).filter(TradeEvent.bot_id.in_(list(bot_ids)))
    if parsed_markets:
        q = q.filter(func.upper(TradeEvent.market).in_(parsed_markets))

    keys: set[str] = set()
    for ev in q.all():
        if ev.order_id:
            keys.add(str(ev.order_id))
        if ev.exchange_order_id:
            keys.add(str(ev.exchange_order_id))
        if ev.id:
            keys.add(str(ev.id))
    return keys


def _to_history_row(ev: TradeEvent) -> dict:
    market = str(ev.market or "")
    base_currency, quote_currency = _base_quote_from_market(market)
    price = float(ev.price or 0.0)
    quote_amount = float(ev.quote_amount or 0.0)
    amount = (quote_amount / price) if price > 0 else 0.0
    status = _event_status(str(ev.event_type or ""))
    ts = _event_iso(ev.timestamp)
    order_id = str(ev.order_id or ev.exchange_order_id or ev.id)
    return {
        "id": ev.id,
        "operator_id": _bitvavo_operator_id_from_bot_id(str(ev.bot_id or "")),
        "order_id": order_id,
        "market": market,
        "side": str(ev.side or "").lower(),
        "total": round(quote_amount, 8),
        "amount": round(amount, 12),
        "price": price,
        "fee": round(float(ev.fee_paid_quote or 0.0), 8),
        "date_time": ts,
        "order_type": "Limit",
        "status": status,
        "filled_price": price,
        "filled_amount": round(amount, 12) if status == "filled" else 0.0,
        "transaction_fee": round(float(ev.fee_paid_quote or 0.0), 8),
        "fee_currency": quote_currency,
        "quote_currency": quote_currency,
        "base_currency": base_currency,
        "date_created": ts,
        "date_updated": ts,
    }


def _list_trade_events_for_notifications(
    db: Session,
    *,
    markets: str | None,
    limit: int,
    event_types: set[str],
) -> list[TradeEvent]:
    safe_limit = int(max(1, min(limit, 500)))
    parsed_markets = _parse_market_list(markets)
    bot_ids = _registered_bot_ids(db)
    if not bot_ids:
        return []

    q = (
        db.query(TradeEvent)
        .filter(TradeEvent.bot_id.in_(list(bot_ids)))
        .filter(TradeEvent.event_type.in_(list(event_types)))
        .order_by(TradeEvent.timestamp.desc(), TradeEvent.id.desc())
    )
    if parsed_markets:
        q = q.filter(func.upper(TradeEvent.market).in_(parsed_markets))
    return q.limit(safe_limit).all()


def _ticker_24h_for_market(market: str) -> dict:
    """Fetch 24h ticker for one market and return normalized pricing values."""
    try:
        resp = requests.get(
            "https://api.bitvavo.com/v2/ticker/24h",
            params={"market": market},
            timeout=6,
        )
    except requests.RequestException:
        return {"last_price": 0.0, "change_pct": 0.0}

    if resp.status_code >= 400:
        return {"last_price": 0.0, "change_pct": 0.0}

    payload = resp.json()
    data = payload[0] if isinstance(payload, list) and payload else payload if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return {"last_price": 0.0, "change_pct": 0.0}

    open_price = _to_float(data.get("open"))
    last_price = _to_float(data.get("last"))
    change_pct = ((last_price - open_price) / open_price * 100.0) if open_price > 0 else 0.0
    return {"last_price": last_price, "change_pct": change_pct}


def _to_order_row(order: dict) -> dict:
    """Normalize one Bitvavo order payload for notification tables/modals."""
    market = str(order.get("market", "") or "")
    amount = _to_float(order.get("amount"))
    open_amount = _to_float(order.get("amountRemaining"))
    filled_amount = _to_float(order.get("filledAmount"))
    price = _to_float(order.get("price"))
    trigger_price = _to_float(order.get("triggerAmount"))
    amount_quote = _to_float(order.get("amountQuote"))
    filled_amount_quote = _to_float(order.get("filledAmountQuote"))
    total_amount_quote = amount_quote if amount_quote > 0 else (amount * price)
    open_amount_quote = max(0.0, total_amount_quote - filled_amount_quote)

    created = _iso_from_millis(order.get("created"))
    updated = _iso_from_millis(order.get("updated"))
    status = str(order.get("status", "") or "")
    side = str(order.get("side", "") or "").lower()

    return {
        "id": str(order.get("orderId", "") or order.get("clientOrderId", "") or ""),
        "market": market,
        "order_type": _fmt_order_type(order.get("orderType")),
        "side": side,
        "status": status,
        "total": round(total_amount_quote, 8),
        "trigger_price": trigger_price,
        "limit_price": price,
        "total_amount": round(total_amount_quote, 8),
        "open_amount": open_amount if open_amount > 0 else open_amount_quote,
        "filled_amount": filled_amount,
        "amount": amount,
        "price": price,
        "filled_price": _to_float(order.get("filledPrice")) or price,
        "transaction_fee": _to_float(order.get("filledFee")),
        "fee_currency": str(order.get("filledFeeCurrency", "") or "EUR"),
        "base_currency": market.split("-", 1)[0] if "-" in market else "",
        "quote_currency": market.split("-", 1)[1] if "-" in market else "EUR",
        "date_time": updated or created,
        "date_created": created,
        "date_updated": updated or created,
        "order_id": str(order.get("orderId", "") or ""),
    }


def _get_all_orders(limit: int = 500) -> list[dict]:
    """Fetch recent private orders across markets."""
    try:
        return _ws_list("privateGetOrders", {"limit": int(max(1, min(limit, 500)))})
    except Exception:
        return []


@router.get("/market/notifications/balance")
def notifications_balance(db: DbSession) -> dict:
    """Return balance rows by proxying approved online agents."""
    now = _time.time()
    cached_rows = _get_cached_proxy_balance_rows(now)
    if cached_rows is not None:
        return {"rows": cached_rows}

    rows = _proxy_notifications_rows(db, "/agent/notifications/balance")
    if rows:
        rows.sort(key=lambda row: row.get("euro_value", 0.0), reverse=True)
        _set_proxy_balance_rows(now, rows)
        return {"rows": rows}

    stale_rows = _get_stale_proxy_balance_rows()
    return {"rows": stale_rows or []}


@router.get("/market/notifications/open-orders")
def notifications_open_orders(db: DbSession, markets: str | None = None) -> dict:
    """Return open orders by proxying approved online agents."""
    params = {"markets": markets} if markets else None
    rows = _proxy_notifications_rows(db, "/agent/notifications/open-orders", params=params)
    rows = _filter_rows_by_known_operator_ids(rows, _registered_operator_ids(db))
    rows.sort(key=lambda row: row.get("date_time", ""), reverse=True)
    return {"rows": rows}


@router.get("/market/notifications/order-history")
def notifications_order_history(db: DbSession, markets: str | None = None, limit: int = 500) -> dict:
    """Return order history rows from pushed bot events (fallback to agent proxy)."""
    safe_limit = int(max(1, min(limit, 500)))
    event_rows = _list_trade_events_for_notifications(
        db,
        markets=markets,
        limit=safe_limit,
        event_types={"order_filled", "order_cancelled"},
    )
    rows = [_to_history_row(ev) for ev in event_rows]

    known_operator_ids = _registered_operator_ids(db)

    deduped: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("order_id") or row.get("id") or "")
        if not key:
            key = json.dumps(row, sort_keys=True, separators=(",", ":"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    deduped = _filter_rows_by_known_operator_ids(deduped, known_operator_ids)

    if deduped:
        deduped.sort(key=lambda row: row.get("date_time", ""), reverse=True)
        return {"rows": deduped[:safe_limit]}

    params: dict[str, str] = {"limit": str(safe_limit)}
    if markets:
        params["markets"] = markets
    rows = _proxy_notifications_rows(db, "/agent/notifications/order-history", params=params)
    rows = [
        row
        for row in rows
        if str(row.get("status", "")).strip().lower() not in {"new", "awaitingtrigger", "partiallyfilled"}
    ]
    rows = _filter_rows_by_known_operator_ids(rows, _registered_operator_ids(db))
    rows.sort(key=lambda row: row.get("date_time", ""), reverse=True)
    return {"rows": rows[:safe_limit]}


@router.get("/market/notifications/trade-history")
def notifications_trade_history(db: DbSession, markets: str | None = None, limit: int = 500) -> dict:
    """Return filled trade history from pushed bot events (fallback to agent proxy)."""
    event_rows = _list_trade_events_for_notifications(
        db,
        markets=markets,
        limit=limit,
        event_types={"order_filled"},
    )
    if event_rows:
        rows = [_to_history_row(ev) for ev in event_rows]
        rows.sort(key=lambda row: row.get("date_time", ""), reverse=True)
        return {"rows": rows}

    params: dict[str, str] = {"limit": str(int(max(1, min(limit, 500))))}
    if markets:
        params["markets"] = markets
    rows = _proxy_notifications_rows(db, "/agent/notifications/trade-history", params=params)
    rows.sort(key=lambda row: row.get("date_time", ""), reverse=True)
    return {"rows": rows}


@router.get("/balance", responses={500: {"description": "Credentials missing"}, 502: {"description": "API failure"}})
def get_balance(symbol: str) -> dict:
    """
    Proxy a balance query to the Bitvavo REST API using HMAC authentication.

    :param symbol: The currency symbol to query (e.g. 'BTC', 'EUR').
    :return: Dict with symbol, available, and inOrder amounts.
    :raises HTTPException: 500 if credentials missing, 502 on API failure.
    """
    url_path = f"/v2/balance?symbol={symbol}"
    resp = _bitvavo_signed_get(url_path)

    if resp.status_code == 401 or resp.status_code == 403:
        return {"symbol": symbol, "available": "0", "inOrder": "0"}

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Bitvavo returned {resp.status_code}: {resp.text}")

    payload = resp.json()
    if isinstance(payload, list):
        if not payload:
            return {"symbol": symbol, "available": "0", "inOrder": "0"}
        entry = payload[0]
    elif isinstance(payload, dict):
        if "errorCode" in payload:
            return {"symbol": symbol, "available": "0", "inOrder": "0"}
        entry = payload
    else:
        return {"symbol": symbol, "available": "0", "inOrder": "0"}

    return {
        "symbol": entry.get("symbol", symbol),
        "available": entry.get("available", "0"),
        "inOrder": entry.get("inOrder", "0"),
    }


@router.get("/market/summary", responses={404: {"description": "Market not found"}, 502: {"description": "API failure"}})
def market_summary(market: str) -> dict:
    """
    Return 24h summary stats for a market from the Bitvavo REST API.

    :param market: The market pair (e.g. 'BTC-EUR').
    :return: Dict with last_price, open_24h, diff, and volume stats.
    :raises HTTPException: 502 on API failure, 404 if market not found.
    """
    try:
        response = requests.get(
            "https://api.bitvavo.com/v2/ticker/24h",
            params={"market": market},
            timeout=6,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch market data: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Bitvavo returned {response.status_code}: {response.text}")

    payload = response.json()
    if isinstance(payload, list):
        if not payload:
            raise HTTPException(status_code=404, detail="Market not found")
        data = payload[0]
    elif isinstance(payload, dict):
        data = payload
    else:
        raise HTTPException(status_code=502, detail="Unexpected market response format")

    try:
        open_price = float(data.get("open", 0.0))
        last_price = float(data.get("last", 0.0))
        volume_quote = float(data.get("volumeQuote", 0.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Invalid market values: {exc}") from exc

    diff_abs = last_price - open_price
    diff_pct = (diff_abs / open_price * 100.0) if open_price > 0 else 0.0

    return {
        "market": data.get("market", market),
        "last_price": last_price,
        "open_24h": open_price,
        "diff_24h_abs": diff_abs,
        "diff_24h_pct": diff_pct,
        "volume_24h_base": float(data.get("volume", 0.0) or 0.0),
        "volume_24h_quote": volume_quote,
    }


@router.get("/markets", responses={502: {"description": "API failure"}})
def list_markets(status: str = "trading") -> list[dict]:
    """
    Return all Bitvavo markets filtered by status.

    :param status: Market status filter (default 'trading').
    :return: Sorted list of market dicts with market, base, quote, and status.
    :raises HTTPException: 502 on API failure.
    """
    try:
        response = requests.get("https://api.bitvavo.com/v2/markets", timeout=8)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch markets: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Bitvavo returned {response.status_code}: {response.text}")

    payload = response.json()
    if not isinstance(payload, list):
        raise HTTPException(status_code=502, detail="Unexpected markets response format")

    normalized = []
    for item in payload:
        if not isinstance(item, dict):
            continue

        market_status = str(item.get("status", "")).lower()
        if status and market_status != status.lower():
            continue

        market_symbol = item.get("market")
        if not market_symbol:
            continue

        normalized.append(
            {
                "market": market_symbol,
                "base": item.get("base"),
                "quote": item.get("quote"),
                "status": item.get("status"),
                "min_order_in_quote_asset": item.get("minOrderInQuoteAsset"),
                "min_order_in_base_asset": item.get("minOrderInBaseAsset"),
            }
        )

    normalized.sort(key=lambda x: x["market"])
    return normalized


@router.get("/market/price-range", responses={502: {"description": "API failure"}})
def market_price_range(market: str, days: int = 7) -> dict:
    """
    Return the average daily high/low for a market over the last *days* days.

    Uses the Bitvavo public ``/v2/candles`` endpoint with ``1d`` interval.

    :param market: Market pair (e.g. ``'BTC-EUR'``).
    :param days: Number of days to look back (1–90, default 7).
    :return: Dict with ``avg_high``, ``avg_low``, ``min_low``, ``max_high``,
             and the raw daily ``candles`` list.
    """
    days = max(1, min(days, 90))

    try:
        resp = requests.get(
            "https://api.bitvavo.com/v2/candles",
            params={"market": market, "interval": "1d", "limit": days},
            timeout=8,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch candles: {exc}") from exc

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Bitvavo returned {resp.status_code}: {resp.text}")

    payload = resp.json()
    if not isinstance(payload, list) or not payload:
        raise HTTPException(status_code=502, detail="No candle data returned")

    # Bitvavo candles: [timestamp, open, high, low, close, volume]
    highs: list[float] = []
    lows: list[float] = []
    for candle in payload:
        if not isinstance(candle, list) or len(candle) < 4:
            continue
        try:
            highs.append(float(candle[2]))
            lows.append(float(candle[3]))
        except (ValueError, TypeError):
            continue

    if not highs or not lows:
        raise HTTPException(status_code=502, detail="Could not parse candle data")

    return {
        "market": market,
        "days": days,
        "avg_high": sum(highs) / len(highs),
        "avg_low": sum(lows) / len(lows),
        "min_low": min(lows),
        "max_high": max(highs),
    }


@router.get("/market/fees")
def market_fees(market: str | None = None, quote: str | None = None) -> dict:
    """Return Bitvavo account + market-specific trading fees.

    Uses websocket private actions:
    - ``privateGetFees`` for market/category-aware maker/taker fees
    - ``privateGetAccount`` for account fee snapshot (includes nested ``fees``)

    The frontend uses ``applied_fee_rate`` (maker) as the default fee for
    static-grid preview, because grid orders are placed as post-only limits.
    """
    normalized_market = str(market or "").strip().upper()
    normalized_quote = str(quote or "").strip().upper()
    if not normalized_quote and "-" in normalized_market:
        normalized_quote = normalized_market.split("-", 1)[1]

    fee_request: dict[str, str] = {}
    if normalized_market:
        fee_request["market"] = normalized_market
    if normalized_quote in {"EUR", "USDC"}:
        fee_request["quote"] = normalized_quote

    try:
        market_fee_resp = _bitvavo_private_ws_call("privateGetFees", fee_request)
        account_resp = _bitvavo_private_ws_call("privateGetAccount", {})
    except Exception as exc:
        return {
            "available": False,
            "market": normalized_market or None,
            "quote": normalized_quote or None,
            "applied_fee_type": "maker",
            "applied_fee_rate": 0.0,
            "message": f"Could not fetch Bitvavo fees: {exc}",
        }

    account_fees = account_resp.get("fees") if isinstance(account_resp.get("fees"), dict) else {}

    maker_market = _to_float(market_fee_resp.get("maker"))
    taker_market = _to_float(market_fee_resp.get("taker"))
    maker_account = _to_float(account_fees.get("maker"))
    taker_account = _to_float(account_fees.get("taker"))
    volume_30d = _to_float(market_fee_resp.get("volume") or account_fees.get("volume"))

    applied_fee_rate = maker_market or maker_account
    applied_fee_type = "maker"

    return {
        "available": True,
        "market": normalized_market or None,
        "quote": normalized_quote or None,
        "tier": str(market_fee_resp.get("tier", "")) or None,
        "volume_30d_eur": volume_30d,
        "market_maker_fee_rate": maker_market,
        "market_taker_fee_rate": taker_market,
        "account_maker_fee_rate": maker_account,
        "account_taker_fee_rate": taker_account,
        "applied_fee_type": applied_fee_type,
        "applied_fee_rate": applied_fee_rate,
    }
