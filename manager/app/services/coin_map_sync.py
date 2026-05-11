"""Periodic downloader for coin_map.json used by market icon rendering."""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

import requests

from manager.app.services.runtime_settings import get_float, get_int, get_setting

logger = logging.getLogger(__name__)


def _coin_map_output_path() -> Path:
    base = Path(__file__).resolve().parents[1] / "static" / "assets"
    base.mkdir(parents=True, exist_ok=True)
    return base / "coin_map.json"


def _validate_payload(payload: object) -> list[dict]:
    if not isinstance(payload, list):
        raise ValueError("coin_map payload is not a list")
    normalized: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol", "")).strip()
        img_url = str(item.get("img_url", "")).strip()
        if not symbol or not img_url:
            continue
        normalized.append(
            {
                "name": str(item.get("name", "")).strip(),
                "symbol": symbol,
                "slug": str(item.get("slug", "")).strip(),
                "img_url": img_url,
            }
        )
    if not normalized:
        raise ValueError("coin_map payload has no valid records")
    return normalized


def sync_coin_map_once() -> bool:
    """Download and atomically replace the local coin_map.json file."""
    try:
        response = requests.get(
            get_setting(
                "COIN_MAP_SOURCE_URL",
                "https://raw.githubusercontent.com/ErikThiart/cryptocurrency-icons/refs/heads/master/coin_map.json",
            ),
            timeout=max(1.0, float(get_float("COIN_MAP_HTTP_TIMEOUT_SECONDS", 20.0))),
        )
        response.raise_for_status()
        payload = response.json()
        normalized = _validate_payload(payload)

        output_path = _coin_map_output_path()
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(output_path.parent)) as tmp:
            json.dump(normalized, tmp, ensure_ascii=True)
            tmp.flush()
            os.fsync(tmp.fileno())
            tmp_name = tmp.name
        Path(tmp_name).replace(output_path)

        logger.info("coin_map sync completed: %s records", len(normalized))
        return True
    except Exception as exc:
        logger.warning("coin_map sync failed: %s", exc)
        return False


def coin_map_sync_loop() -> None:
    """Run immediate sync at startup, then continue with fixed interval."""
    sync_coin_map_once()
    while True:
        time.sleep(max(300, get_int("COIN_MAP_SYNC_INTERVAL_SECONDS", 86400)))
        sync_coin_map_once()
