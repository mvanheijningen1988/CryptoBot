"""Market data proxy endpoints: balance, 24h summary, and market listing."""
from __future__ import annotations

import json
import hashlib
import hmac
import os
import time as _time

import requests
import websocket
from fastapi import HTTPException
from fastapi.routing import APIRouter

router = APIRouter()


def _bitvavo_private_ws_call(action: str, payload: dict | None = None, timeout_seconds: float = 8.0) -> dict:
    """Execute a private Bitvavo websocket action and return its response object.

    The call authenticates on a short-lived websocket connection and waits for
    the response matching a generated ``requestId``.
    """
    api_key = os.getenv("BITVAVO_API_KEY", "")
    api_secret = os.getenv("BITVAVO_API_SECRET", "")
    if not api_key or not api_secret:
        raise RuntimeError("Bitvavo API credentials not configured")

    ws = None
    req_id = int(_time.time() * 1000)
    auth_req_id = req_id - 1
    deadline = _time.time() + timeout_seconds
    body = payload or {}

    try:
        ws = websocket.create_connection("wss://ws.bitvavo.com/v2/", timeout=timeout_seconds)
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


@router.get("/balance", responses={500: {"description": "Credentials missing"}, 502: {"description": "API failure"}})
def get_balance(symbol: str) -> dict:
    """
    Proxy a balance query to the Bitvavo REST API using HMAC authentication.

    :param symbol: The currency symbol to query (e.g. 'BTC', 'EUR').
    :return: Dict with symbol, available, and inOrder amounts.
    :raises HTTPException: 500 if credentials missing, 502 on API failure.
    """
    api_key = os.getenv("BITVAVO_API_KEY", "")
    api_secret = os.getenv("BITVAVO_API_SECRET", "")
    if not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Bitvavo API credentials not configured")

    timestamp = str(int(_time.time() * 1000))
    method = "GET"
    url_path = f"/v2/balance?symbol={symbol}"
    body = ""
    sig_string = timestamp + method + url_path + body
    signature = hmac.new(
        api_secret.encode("utf-8"),
        sig_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "BITVAVO-ACCESS-KEY": api_key,
        "BITVAVO-ACCESS-SIGNATURE": signature,
        "BITVAVO-ACCESS-TIMESTAMP": timestamp,
    }

    try:
        resp = requests.get(f"https://api.bitvavo.com{url_path}", headers=headers, timeout=6)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch balance: {exc}") from exc

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
