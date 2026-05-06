"""Market data proxy endpoints: balance, 24h summary, and market listing."""
from __future__ import annotations

import hashlib
import hmac
import os
import time as _time

import requests
from fastapi import HTTPException
from fastapi.routing import APIRouter

router = APIRouter()


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
            }
        )

    normalized.sort(key=lambda x: x["market"])
    return normalized
