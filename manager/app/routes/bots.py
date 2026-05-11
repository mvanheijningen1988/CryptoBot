"""Bot CRUD, start/stop, budget update, and metrics push endpoints."""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Body, Depends, HTTPException
from fastapi.routing import APIRouter
import requests
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from common.diagnostics import debug_log, get_correlation_id, scoped_context, trace_log

logger = logging.getLogger(__name__)

from manager.app.database import get_db
from manager.app.events import (
    add_agent_event,
    add_equity_point,
    add_trade_event,
    delete_trade_events_for_bot,
    get_trade_events,
)
from manager.app.models import Agent, Bot, TradeEvent
from manager.app.schemas import (
    BotCreateRequest,
    DeleteBotRequest,
    BotResponse,
    MetricsPushRequest,
    MoveBotRequest,
    StartBotRequest,
    UpdateBudgetRequest,
)
from manager.app.services.agent_client import post_json
from manager.app.services.agent_ws_bus import send_agent_command_ws

router = APIRouter()

DbSession = Annotated[Session, Depends(get_db)]

_BOT_NOT_FOUND = "Bot not found"
_AGENT_NOT_FOUND = "Agent not found"
_AGENT_NOT_APPROVED = "Agent is not approved"
_ACTIVE_BOT_STATUSES = ("initializing", "running")


def _load_bot_state_flags(bot: Bot) -> dict:
    """Load manager-side bot state flags from state_json."""
    try:
        data = json.loads(bot.state_json or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _load_saved_runner_state(bot: Bot) -> dict | None:
    """Load the persisted runner state from the dedicated full-state payload."""
    for raw_state in (bot.full_state_json, bot.state_json):
        try:
            data = json.loads(raw_state or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        runner_state = data.get("runner_state")
        if isinstance(runner_state, dict):
            return runner_state
        if any(key in data for key in ("level_index", "open_orders", "filled_buys", "filled_amounts", "initial_equity", "trade_count")):
            return data
    return None


def _set_full_state(bot: Bot, snapshot: object | None, runner_state: object | None) -> None:
    """Persist the bot's full runtime state separately from manager flags."""
    payload: dict[str, object] = {}
    if snapshot is not None:
        payload["snapshot"] = snapshot.model_dump(mode="json") if hasattr(snapshot, "model_dump") else snapshot
    if runner_state is not None:
        payload["runner_state"] = runner_state.model_dump(mode="json") if hasattr(runner_state, "model_dump") else runner_state
    bot.full_state_json = json.dumps(payload)


def _build_open_orders_from_saved_state(bot: Bot) -> dict:
    """Reconstruct open orders from the last persisted runner state."""
    saved_state = _load_saved_runner_state(bot) or {}
    config = json.loads(bot.config_json or "{}")
    grid = config.get("grid", {})
    lower_price = float(grid.get("lower_price") or 0.0)
    upper_price = float(grid.get("upper_price") or 0.0)
    levels = int(grid.get("levels") or 0)
    order_size_quote = float(grid.get("order_size_quote") or 0.0)
    filled_amounts = saved_state.get("filled_amounts") or {}
    open_orders = []

    if levels >= 2 and upper_price > lower_price:
        step = (upper_price - lower_price) / (levels - 1)
        for raw_level, side in (saved_state.get("open_orders") or {}).items():
            try:
                level_index = int(raw_level)
            except (TypeError, ValueError):
                continue
            if level_index < 0 or level_index >= levels:
                continue
            filled_quote = float(filled_amounts.get(str(level_index), filled_amounts.get(level_index, 0.0)) or 0.0)
            open_orders.append({
                "level": level_index,
                "price": round(lower_price + level_index * step, 6),
                "side": str(side),
                "quote_amount": order_size_quote,
                "filled_quote": round(filled_quote, 6),
            })

    return {"bot_id": bot.id, "orders": sorted(open_orders, key=lambda item: item["level"])}


def _set_manual_stop_flag(bot: Bot, value: bool) -> None:
    """Persist the manual stop marker for stale-metrics protection."""
    flags = _load_bot_state_flags(bot)
    flags["manual_stop"] = bool(value)
    bot.state_json = json.dumps(flags)


def _config_value(obj: object, key: str, default: object = None) -> object:
    """Read a config field from either a model-like object or a dict."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _fetch_bitvavo_market_limits(market: str) -> dict[str, float | str]:
    """Fetch minimum order constraints for one Bitvavo market."""
    try:
        response = requests.get(
            "https://api.bitvavo.com/v2/markets",
            params={"market": market},
            timeout=6,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch Bitvavo market limits: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Bitvavo returned {response.status_code}: {response.text}")

    payload = response.json()
    if isinstance(payload, list):
        if not payload or not isinstance(payload[0], dict):
            raise HTTPException(status_code=404, detail=f"Bitvavo market not found: {market}")
        item = payload[0]
    elif isinstance(payload, dict):
        item = payload
    else:
        raise HTTPException(status_code=502, detail="Unexpected Bitvavo markets response format")

    try:
        return {
            "market": str(item.get("market") or market),
            "min_quote": float(item.get("minOrderInQuoteAsset") or 0.0),
            "min_base": float(item.get("minOrderInBaseAsset") or 0.0),
        }
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Invalid Bitvavo market limits: {exc}") from exc


def _validate_live_order_size(config: object) -> None:
    """Reject live bots whose order size is below Bitvavo minimums."""
    mode = _config_value(config, "mode", "")
    if mode != "live":
        return

    market = str(_config_value(config, "market", "") or "").upper()
    quote_currency = str(_config_value(config, "quote_currency", "") or "QUOTE")
    base_currency = str(_config_value(config, "base_currency", "") or "BASE")
    grid = _config_value(config, "grid", {})
    order_size_quote = float(_config_value(grid, "order_size_quote", 0.0) or 0.0)
    highest_grid_price = max(
        float(_config_value(grid, "lower_price", 0.0) or 0.0),
        float(_config_value(grid, "upper_price", 0.0) or 0.0),
    )

    limits = _fetch_bitvavo_market_limits(market)
    min_quote = float(limits["min_quote"] or 0.0)
    min_base = float(limits["min_base"] or 0.0)
    min_quote_from_base = min_base * highest_grid_price if min_base > 0 and highest_grid_price > 0 else 0.0
    minimum_required_quote = max(min_quote, min_quote_from_base)

    if order_size_quote + 1e-12 >= minimum_required_quote:
        return

    detail = (
        f"Order size {order_size_quote:.8f} {quote_currency} is below the Bitvavo minimum for {market}. "
        f"Minimum required order size is {minimum_required_quote:.8f} {quote_currency}"
    )
    extras: list[str] = []
    if min_quote > 0:
        extras.append(f"min quote: {min_quote:.8f} {quote_currency}")
    if min_base > 0:
        extras.append(
            f"min base: {min_base:.8f} {base_currency} (={min_quote_from_base:.8f} {quote_currency} at max grid price {highest_grid_price:.8f})"
        )
    if extras:
        detail += f" ({'; '.join(extras)})"
    raise HTTPException(status_code=400, detail=detail)


def _resolve_fee_rate_for_bot(bot: Bot) -> float:
    """Return the configured fee rate for the bot mode."""
    try:
        config = json.loads(bot.config_json or "{}")
        configured_fee_rate = float(config.get("fee_rate", 0) or 0)
    except (TypeError, ValueError, json.JSONDecodeError):
        configured_fee_rate = 0.0

    if configured_fee_rate > 0:
        return configured_fee_rate

    if bot.mode == "simulation":
        return float(os.getenv("SIM_MAKER_FEE_RATE", os.getenv("SIM_FEE_RATE", "0.0")))
    return float(
        os.getenv(
            "LIVE_MAKER_FEE_RATE",
            os.getenv("LIVE_FEE_RATE", os.getenv("SIM_MAKER_FEE_RATE", os.getenv("SIM_FEE_RATE", "0.0"))),
        )
    )


def _trade_based_pnl(metrics: dict[str, object], starting_equity: float | None = None) -> float:
    """Return the PnL value that should be shown in the dashboard.

    Prefer mark-to-market PnL when the starting equity baseline is known.
    """
    total_equity = float(metrics.get("total_equity_quote", 0.0) or 0.0)
    if starting_equity is not None:
        return total_equity - float(starting_equity)
    realized = float(metrics.get("realized_pnl_quote", 0.0) or 0.0)
    unrealized = float(metrics.get("unrealized_pnl_quote", 0.0) or 0.0)
    if abs(unrealized) > 0:
        return realized + unrealized
    try:
        return realized
    except (TypeError, ValueError):
        return 0.0


def _count_filled_events(bot_id: str) -> int:
    """Count persisted filled order events for one bot."""
    from manager.app.database import SessionLocal
    from manager.app.models import TradeEvent

    db = SessionLocal()
    try:
        return int(
            db.query(TradeEvent)
            .filter(TradeEvent.bot_id == bot_id, TradeEvent.event_type == "order_filled")
            .count()
        )
    except SQLAlchemyError:
        return 0
    finally:
        db.close()


def _sum_filled_trade_pnl(bot_id: str, db: Session | None = None) -> float:
    """Return the total persisted trade PnL across filled events for one bot."""
    owns_session = db is None
    if db is None:
        from manager.app.database import SessionLocal
        db = SessionLocal()

    try:
        total = (
            db.query(func.coalesce(func.sum(TradeEvent.trade_pnl), 0.0))
            .filter(TradeEvent.bot_id == bot_id, TradeEvent.event_type == "order_filled")
            .scalar()
        )
        return float(total or 0.0)
    except SQLAlchemyError:
        return 0.0
    finally:
        if owns_session:
            db.close()


def _compute_live_pnl_state_from_fills(
    bot_id: str,
    current_price: float,
    db: Session | None = None,
) -> tuple[float, float, float, float]:
    """Compute realized+unrealized PnL state from filled events using FIFO lots.

    Returns a tuple of:
    - realized_pnl_quote
    - unrealized_pnl_quote
    - open_base_amount
    - average_buy_price_for_open_base
    """
    if current_price <= 0:
        return 0.0, 0.0, 0.0, 0.0

    owns_session = db is None
    if db is None:
        from manager.app.database import SessionLocal
        db = SessionLocal()

    try:
        fills = (
            db.query(TradeEvent)
            .filter(TradeEvent.bot_id == bot_id, TradeEvent.event_type == "order_filled")
            .order_by(TradeEvent.timestamp.asc(), TradeEvent.id.asc())
            .all()
        )
    except SQLAlchemyError:
        return 0.0, 0.0, 0.0, 0.0
    finally:
        if owns_session:
            db.close()

    # FIFO lots: each entry tracks remaining base qty and quote cost basis.
    lots: list[dict[str, float]] = []
    realized_pnl = 0.0

    for fill in fills:
        realized_pnl += _apply_fill_to_open_lots(lots, fill)

    open_base = sum(max(0.0, float(l.get("qty", 0.0) or 0.0)) for l in lots)
    open_cost_quote = sum(max(0.0, float(l.get("cost", 0.0) or 0.0)) for l in lots)
    if open_base <= 0:
        return realized_pnl, 0.0, 0.0, 0.0

    avg_buy_price = open_cost_quote / open_base if open_base > 0 else 0.0
    open_value_quote = open_base * float(current_price)
    unrealized_pnl = open_value_quote - open_cost_quote
    return realized_pnl, unrealized_pnl, open_base, avg_buy_price


def _reconstruct_live_balances_from_fills(
    bot_id: str,
    start_quote: float,
    start_base: float,
    db: Session | None = None,
) -> tuple[float, float, int] | None:
    """Rebuild bot-scoped balances from persisted live fill events."""
    owns_session = db is None
    if db is None:
        from manager.app.database import SessionLocal
        db = SessionLocal()

    try:
        fills = (
            db.query(TradeEvent)
            .filter(TradeEvent.bot_id == bot_id, TradeEvent.event_type == "order_filled")
            .order_by(TradeEvent.timestamp.asc(), TradeEvent.id.asc())
            .all()
        )
    except SQLAlchemyError:
        return None
    finally:
        if owns_session:
            db.close()

    quote_balance = float(start_quote)
    base_balance = float(start_base)

    for fill in fills:
        side = str(fill.side or "").lower()
        quote_amount = float(fill.quote_amount or 0.0)
        fill_price = float(fill.price or 0.0)
        fee_paid_quote = float(fill.fee_paid_quote or 0.0)
        if quote_amount <= 0 or fill_price <= 0:
            continue

        base_amount = quote_amount / fill_price
        if side == "buy":
            quote_balance -= quote_amount
            base_balance += max(0.0, base_amount - (fee_paid_quote / fill_price if fee_paid_quote > 0 else 0.0))
        elif side == "sell":
            # Keep signed base inventory so unmatched sells cannot create artificial quote profit.
            base_balance -= base_amount
            quote_balance += max(0.0, quote_amount - fee_paid_quote)

    return quote_balance, base_balance, len(fills)


def _list_filled_events(bot_id: str, db: Session | None = None) -> list[TradeEvent]:
    """Return filled-order events for one bot in chronological order."""
    owns_session = db is None
    if db is None:
        from manager.app.database import SessionLocal
        db = SessionLocal()

    try:
        return (
            db.query(TradeEvent)
            .filter(TradeEvent.bot_id == bot_id, TradeEvent.event_type == "order_filled")
            .order_by(TradeEvent.timestamp.asc(), TradeEvent.id.asc())
            .all()
        )
    except SQLAlchemyError:
        return []
    finally:
        if owns_session:
            db.close()


def _timestamp_to_epoch_seconds(ts: object) -> float | None:
    """Parse timestamps from DB datetimes or ISO strings into UTC epoch seconds."""
    if isinstance(ts, datetime):
        dt = ts
    elif isinstance(ts, str):
        raw = ts.strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


_EQUITY_AGGREGATION_TO_SECONDS: dict[str, int] = {
    "1m": 60,
    "5m": 300,
    "10m": 600,
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "2h": 7200,
    "4h": 14400,
    "6h": 21600,
    "8h": 28800,
    "12h": 43200,
    "1d": 86400,
    "1w": 604800,
}


def _normalize_equity_aggregation(raw: str | None) -> str:
    """Normalize and validate aggregation interval tokens."""
    value = str(raw or "1m").strip().lower()
    if value in _EQUITY_AGGREGATION_TO_SECONDS or value == "1mo":
        return value
    return "1m"


def _bucket_start_for_aggregation(ts: object, aggregation: str) -> tuple[str, float] | None:
    """Return bucket ISO timestamp and epoch start for a point timestamp."""
    epoch = _timestamp_to_epoch_seconds(ts)
    if epoch is None:
        return None

    if aggregation == "1mo":
        if isinstance(ts, datetime):
            dt = ts
        elif isinstance(ts, str):
            raw = ts.strip()
            if raw.endswith("Z"):
                raw = f"{raw[:-1]}+00:00"
            try:
                dt = datetime.fromisoformat(raw)
            except ValueError:
                return None
        else:
            return None

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        dt = dt.astimezone(UTC)
        month_start = dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return month_start.isoformat().replace("+00:00", "Z"), month_start.timestamp()

    bucket_seconds = _EQUITY_AGGREGATION_TO_SECONDS.get(aggregation, 300)
    bucket_epoch = int(epoch // bucket_seconds) * bucket_seconds
    bucket_dt = datetime.fromtimestamp(bucket_epoch, tz=UTC)
    return bucket_dt.isoformat().replace("+00:00", "Z"), float(bucket_epoch)


def _aggregate_equity_points(points: list[dict[str, object]], aggregation: str) -> list[dict[str, object]]:
    """Aggregate points by taking the latest value inside each time bucket."""
    if not points:
        return []

    normalized = _normalize_equity_aggregation(aggregation)
    buckets: dict[str, dict[str, object]] = {}

    for point in points:
        point_ts = point.get("t")
        bucket = _bucket_start_for_aggregation(point_ts, normalized)
        point_epoch = _timestamp_to_epoch_seconds(point_ts)
        if bucket is None or point_epoch is None:
            continue
        bucket_iso, bucket_epoch = bucket
        current = buckets.get(bucket_iso)
        if current is None or float(current.get("_point_epoch", -1.0)) <= point_epoch:
            buckets[bucket_iso] = {
                "t": bucket_iso,
                "v": float(point.get("v", 0.0) or 0.0),
                "p": float(point.get("p", 0.0) or 0.0),
                "_bucket_epoch": bucket_epoch,
                "_point_epoch": point_epoch,
            }

    aggregated = sorted(buckets.values(), key=lambda item: float(item.get("_bucket_epoch", 0.0)))
    return [{"t": p["t"], "v": p["v"], "p": p["p"]} for p in aggregated]


def _build_persisted_equity_points(bot_id: str, db: Session) -> list[dict[str, object]]:
    """Build equity points from persisted TradeEvent rows."""
    rows = (
        db.query(TradeEvent)
        .filter(TradeEvent.bot_id == bot_id)
        .order_by(TradeEvent.timestamp.asc(), TradeEvent.id.asc())
        .all()
    )

    points: dict[str, dict[str, object]] = {}
    for row in rows:
        if row.timestamp is None:
            continue
        total_equity = float(row.total_equity or 0.0)
        if total_equity <= 0:
            continue
        ts = row.timestamp.isoformat().replace("+00:00", "Z")
        points[ts] = {
            "t": ts,
            "v": total_equity,
            "p": float(row.price or 0.0),
        }

    return sorted(points.values(), key=lambda p: str(p.get("t", "")))


def _get_bot_equity_points(bot: Bot, db: Session) -> list[dict[str, object]]:
    """Return a durable equity series by combining persisted and live in-memory points."""
    from manager.app.events import EQUITY_HISTORY, EQUITY_HISTORY_LOCK

    config = json.loads(bot.config_json or "{}") if bot else {}
    budget = config.get("budget", {}) if isinstance(config, dict) else {}

    with EQUITY_HISTORY_LOCK:
        memory_points = list(EQUITY_HISTORY.get(bot.id, []))

    if bot.mode == "live":
        start_quote = float(budget.get("quote_budget", 0.0) or 0.0)
        start_base = float(budget.get("base_budget", 0.0) or 0.0)

        # Persistent-first for live mode: reconstruct from DB fills so the
        # trend does not reset when in-memory history is truncated/restarted.
        fill_points = _build_live_equity_points_from_fills(bot.id, start_quote, start_base, db)

        merged: dict[str, dict[str, object]] = {
            str(p.get("t", "")): {
                "t": p.get("t"),
                "v": float(p.get("v", 0.0) or 0.0),
                "p": float(p.get("p", 0.0) or 0.0),
            }
            for p in fill_points
            if p.get("t")
        }

        # Memory points are optional timeline anchors for denser price samples.
        for p in memory_points:
            t = str(p.get("t", ""))
            if not t:
                continue
            merged[t] = {
                "t": t,
                "v": float(p.get("v", 0.0) or 0.0),
                "p": float(p.get("p", 0.0) or 0.0),
            }

        timeline = sorted(
            merged.values(),
            key=lambda p: float(_timestamp_to_epoch_seconds(p.get("t")) or 0.0),
        )
        if timeline:
            return _rebuild_live_equity_points_from_fills(
                bot.id,
                timeline,
                start_quote,
                start_base,
                db,
            )

        # No fills/timeline yet: fall back to memory points if available.
        if memory_points:
            return sorted(
                [
                    {
                        "t": p.get("t"),
                        "v": float(p.get("v", 0.0) or 0.0),
                        "p": float(p.get("p", 0.0) or 0.0),
                    }
                    for p in memory_points
                    if p.get("t")
                ],
                key=lambda p: float(_timestamp_to_epoch_seconds(p.get("t")) or 0.0),
            )
        return []

    points = _build_persisted_equity_points(bot.id, db)

    if memory_points:
        merged: dict[str, dict[str, object]] = {
            str(p.get("t", "")): {"t": p.get("t"), "v": float(p.get("v", 0.0) or 0.0), "p": float(p.get("p", 0.0) or 0.0)}
            for p in points
            if p.get("t")
        }
        for p in memory_points:
            t = str(p.get("t", ""))
            if not t:
                continue
            merged[t] = {
                "t": t,
                "v": float(p.get("v", 0.0) or 0.0),
                "p": float(p.get("p", 0.0) or 0.0),
            }
        points = sorted(
            merged.values(),
            key=lambda p: float(_timestamp_to_epoch_seconds(p.get("t")) or 0.0),
        )

    return points


def _apply_fill_to_balances(quote_balance: float, base_balance: float, fill: TradeEvent) -> tuple[float, float]:
    """Apply one fill event to quote/base balances."""
    side = str(fill.side or "").lower()
    quote_amount = float(fill.quote_amount or 0.0)
    fill_price = float(fill.price or 0.0)
    fee_paid_quote = float(fill.fee_paid_quote or 0.0)
    if quote_amount <= 0 or fill_price <= 0:
        return quote_balance, base_balance

    base_amount = quote_amount / fill_price
    if side == "buy":
        quote_balance -= quote_amount
        base_balance += max(0.0, base_amount - (fee_paid_quote / fill_price if fee_paid_quote > 0 else 0.0))
    elif side == "sell":
        # Keep signed base inventory so chart/equity reconstruction remains value-consistent.
        base_balance -= base_amount
        quote_balance += max(0.0, quote_amount - fee_paid_quote)
    return quote_balance, base_balance


def _apply_fill_to_open_lots(lots: list[dict[str, float]], fill: TradeEvent) -> float:
    """Apply one fill event to FIFO lots and return realized PnL delta."""
    side = str(fill.side or "").lower()
    quote_amount = float(fill.quote_amount or 0.0)
    fill_price = float(fill.price or 0.0)
    fee_paid_quote = float(fill.fee_paid_quote or 0.0)
    if quote_amount <= 0 or fill_price <= 0:
        return 0.0

    base_amount = quote_amount / fill_price
    if side == "buy":
        net_base = max(0.0, base_amount - (fee_paid_quote / fill_price if fee_paid_quote > 0 else 0.0))
        if net_base > 0:
            lots.append({"qty": net_base, "cost": quote_amount})
        return 0.0

    if side == "sell":
        remaining_to_close = max(0.0, base_amount)
        matched_qty = 0.0
        matched_cost = 0.0
        lot_idx = 0
        while remaining_to_close > 1e-12 and lot_idx < len(lots):
            lot = lots[lot_idx]
            lot_qty = float(lot.get("qty", 0.0) or 0.0)
            lot_cost = float(lot.get("cost", 0.0) or 0.0)
            if lot_qty <= 1e-12:
                lot_idx += 1
                continue

            take_qty = min(lot_qty, remaining_to_close)
            unit_cost = lot_cost / lot_qty if lot_qty > 0 else 0.0
            matched_qty += take_qty
            matched_cost += unit_cost * take_qty
            lot["qty"] = lot_qty - take_qty
            lot["cost"] = max(0.0, lot_cost - (unit_cost * take_qty))
            remaining_to_close -= take_qty
            if lot["qty"] <= 1e-12:
                lot_idx += 1

        if base_amount > 1e-12 and matched_qty > 0:
            net_sell_quote = max(0.0, quote_amount - fee_paid_quote)
            matched_ratio = min(1.0, matched_qty / base_amount)
            matched_proceeds = net_sell_quote * matched_ratio
            return matched_proceeds - matched_cost

    return 0.0


def _unrealized_from_open_lots(lots: list[dict[str, float]], point_price: float) -> float:
    """Compute unrealized PnL from remaining open lots at a given price."""
    if point_price <= 0:
        return 0.0
    open_base = sum(max(0.0, float(l.get("qty", 0.0) or 0.0)) for l in lots)
    if open_base <= 0:
        return 0.0
    open_cost_quote = sum(max(0.0, float(l.get("cost", 0.0) or 0.0)) for l in lots)
    return open_base * point_price - open_cost_quote


def _rebuild_live_equity_points_from_fills(
    bot_id: str,
    points: list[dict[str, object]],
    start_quote: float,
    start_base: float,
    db: Session | None = None,
) -> list[dict[str, object]]:
    """Recompute live equity points using dashboard-equity convention.

    Equity convention (same as bots table):
    start budget + cumulative realized trade PnL + unrealized PnL on open lots.
    """
    if not points:
        return points

    fills = _list_filled_events(bot_id, db)
    if not fills:
        return points

    parsed_points: list[tuple[float, dict[str, object]]] = []
    for point in points:
        ts = _timestamp_to_epoch_seconds(point.get("t"))
        if ts is None:
            continue
        parsed_points.append((ts, point))
    if not parsed_points:
        return points

    parsed_points.sort(key=lambda item: item[0])
    parsed_fills: list[tuple[float, TradeEvent]] = []
    for fill in fills:
        ts = _timestamp_to_epoch_seconds(fill.timestamp)
        if ts is None:
            continue
        parsed_fills.append((ts, fill))

    # start_base is intentionally excluded from dashboard-equity convention.
    _ = start_base
    realized_pnl = 0.0
    open_lots: list[dict[str, float]] = []
    fill_idx = 0
    rebuilt: list[dict[str, object]] = []

    for point_ts, point in parsed_points:
        while fill_idx < len(parsed_fills) and parsed_fills[fill_idx][0] <= point_ts:
            _, fill = parsed_fills[fill_idx]
            realized_pnl += _apply_fill_to_open_lots(open_lots, fill)
            fill_idx += 1

        point_price = float(point.get("p", 0.0) or 0.0)
        unrealized_pnl = _unrealized_from_open_lots(open_lots, point_price)
        point_equity = float(start_quote) + realized_pnl + unrealized_pnl

        rebuilt.append({
            "t": point.get("t"),
            "v": point_equity,
            "p": point_price,
        })

    return rebuilt


def _build_live_equity_points_from_fills(
    bot_id: str,
    start_quote: float,
    start_base: float,
    db: Session | None = None,
) -> list[dict[str, object]]:
    """Build live equity series from fills using dashboard-equity convention."""
    fills = _list_filled_events(bot_id, db)
    if not fills:
        return []

    # start_base is intentionally excluded from dashboard-equity convention.
    _ = start_base
    realized_pnl = 0.0
    open_lots: list[dict[str, float]] = []
    points: list[dict[str, object]] = []
    seen_start = False

    for fill in fills:
        fill_price = float(fill.price or 0.0)
        if fill_price <= 0:
            continue
        ts_dt = fill.timestamp if isinstance(fill.timestamp, datetime) else None
        ts = ts_dt.isoformat() if ts_dt else ""
        if ts and not seen_start:
            # Emit a baseline point just before first fill so chart reconstruction
            # contains both pre-fill and post-fill equity values.
            start_ts = (ts_dt - timedelta(seconds=1)).isoformat() if ts_dt else ts
            points.append({
                "t": start_ts,
                "v": float(start_quote),
                "p": fill_price,
            })
            seen_start = True

        realized_pnl += _apply_fill_to_open_lots(open_lots, fill)
        unrealized_pnl = _unrealized_from_open_lots(open_lots, fill_price)

        if ts:
            points.append({
                "t": ts,
                "v": float(start_quote) + realized_pnl + unrealized_pnl,
                "p": fill_price,
            })

    return points


def _normalized_metrics_for_bot(bot: Bot, db: Session | None = None) -> tuple[dict[str, object], float]:
    """Return normalized metrics and starting quote budget for one bot."""
    latest_metrics = json.loads(bot.latest_metrics_json or "{}")
    config = json.loads(bot.config_json or "{}")
    budget = config.get("budget", {}) if isinstance(config, dict) else {}

    start_price = float(
        (config.get("start_price") if isinstance(config, dict) else 0.0)
        or latest_metrics.get("price", 0.0)
        or 0.0
    )
    starting_equity = float(budget.get("quote_budget", 0.0) or 0.0) + float(budget.get("base_budget", 0.0) or 0.0) * start_price

    if bot.mode == "live":
        reconstructed = _reconstruct_live_balances_from_fills(
            bot.id,
            float(budget.get("quote_budget", 0.0) or 0.0),
            float(budget.get("base_budget", 0.0) or 0.0),
            db,
        )
        if reconstructed is not None:
            rec_quote, rec_base, rec_trade_count = reconstructed
            latest_metrics["quote_balance"] = rec_quote
            latest_metrics["base_balance"] = rec_base
            latest_metrics["trade_count"] = rec_trade_count
    else:
        current_trade_count = int(latest_metrics.get("trade_count", 0) or 0)
        if current_trade_count <= 0:
            filled_count = _count_filled_events(bot.id)
            if filled_count > current_trade_count:
                latest_metrics["trade_count"] = filled_count

    # Dashboard convention (requested):
    # - PnL = completed trade result + unrealized result on open base inventory.
    # - Equity = start budget + that total PnL.
    if bot.mode == "live":
        current_price = float(latest_metrics.get("price", 0.0) or 0.0)
        trade_pnl_total, unrealized_pnl, open_base_amount, open_base_avg_buy_price = _compute_live_pnl_state_from_fills(
            bot.id,
            current_price,
            db,
        )
        total_pnl = trade_pnl_total + unrealized_pnl
        total_equity = float(budget.get("quote_budget", 0.0) or 0.0) + total_pnl
        latest_metrics["dashboard_pnl_quote"] = total_pnl
        latest_metrics["realized_pnl_quote"] = trade_pnl_total
        latest_metrics["unrealized_pnl_quote"] = unrealized_pnl
        latest_metrics["total_equity_quote"] = total_equity
        latest_metrics["open_base_amount"] = open_base_amount
        latest_metrics["open_base_avg_buy_price"] = open_base_avg_buy_price
    else:
        price = float(latest_metrics.get("price", 0.0) or 0.0)
        quote_balance = float(latest_metrics.get("quote_balance", 0.0) or 0.0)
        base_balance = float(latest_metrics.get("base_balance", 0.0) or 0.0)
        mtm_equity = quote_balance + base_balance * price
        if mtm_equity > 0:
            latest_metrics["total_equity_quote"] = mtm_equity
            latest_metrics["unrealized_pnl_quote"] = mtm_equity - starting_equity
        latest_metrics["dashboard_pnl_quote"] = _trade_based_pnl(latest_metrics, starting_equity)

    return latest_metrics, float(budget.get("quote_budget", 0.0) or 0.0)


def _build_pair_metrics(bot: Bot, event: object, linked: object | None) -> dict | None:
    """Compute realized grid PnL for a linked buy/sell fill pair."""
    if linked is None:
        return None

    if getattr(event, "side", None) == "buy" and getattr(linked, "side", None) == "sell":
        buy_event, sell_event = event, linked
    elif getattr(event, "side", None) == "sell" and getattr(linked, "side", None) == "buy":
        buy_event, sell_event = linked, event
    else:
        return None

    # Pair PnL is only meaningful once the linked sell is actually filled.
    if getattr(sell_event, "event_type", None) != "order_filled":
        return None

    buy_price = float(getattr(buy_event, "price", 0) or 0)
    sell_price = float(getattr(sell_event, "price", 0) or 0)
    quote_spent = float(getattr(buy_event, "quote_amount", 0) or 0)
    if buy_price <= 0 or sell_price <= 0 or quote_spent <= 0:
        return None

    fee_rate = _resolve_fee_rate_for_bot(bot)
    quantity_base = quote_spent / buy_price
    gross_profit = (sell_price - buy_price) * quantity_base
    quote_received_before_fees = quote_spent * (sell_price / buy_price)
    buy_fee_persisted = float(getattr(buy_event, "fee_paid_quote", 0) or 0)
    sell_fee_persisted = float(getattr(sell_event, "fee_paid_quote", 0) or 0)
    if buy_fee_persisted > 0 or sell_fee_persisted > 0:
        buy_fee = buy_fee_persisted
        sell_fee = sell_fee_persisted
        fee_rate = float(getattr(sell_event, "fee_rate", 0) or getattr(buy_event, "fee_rate", 0) or fee_rate)
    else:
        buy_fee = quote_spent * fee_rate
        sell_fee = quote_received_before_fees * fee_rate
    total_fees = buy_fee + sell_fee
    realized_pnl = gross_profit - total_fees

    return {
        "quantity_base": round(quantity_base, 8),
        "gross_profit_quote": round(gross_profit, 6),
        "total_fees_quote": round(total_fees, 6),
        "realized_pnl_quote": round(realized_pnl, 6),
        "fee_rate": fee_rate,
    }


def _select_least_loaded_agent(db: Session) -> Agent | None:
    """Pick the approved online agent with the fewest assigned bots.

    Only agents whose current bot count is below their capacity are
    considered.  Among those, the agent with the fewest bots wins
    (ties broken by agent id for determinism).

    :param db: Database session.
    :return: The best agent, or ``None`` if none are available.
    """
    from sqlalchemy import func

    agents = (
        db.query(Agent)
        .filter(Agent.status == "online", Agent.approval_status == "approved")
        .all()
    )
    if not agents:
        return None

    # Count running bots per agent
    counts = dict(
        db.query(Bot.assigned_agent_id, func.count(Bot.id))
        .filter(Bot.status.in_(_ACTIVE_BOT_STATUSES), Bot.assigned_agent_id.isnot(None))
        .group_by(Bot.assigned_agent_id)
        .all()
    )

    best: Agent | None = None
    best_count = float("inf")
    for agent in agents:
        n = counts.get(agent.id, 0)
        if n >= agent.capacity:
            continue
        if n < best_count or (n == best_count and best and agent.id < best.id):
            best = agent
            best_count = n
    return best


def bot_to_response(bot: Bot) -> BotResponse:
    """
    Convert a Bot ORM instance to its Pydantic response schema.

    :param bot: The Bot database model instance.
    :return: A BotResponse Pydantic model.
    """
    latest_metrics, _ = _normalized_metrics_for_bot(bot)
    config = json.loads(bot.config_json or "{}")

    return BotResponse(
        id=bot.id,
        name=bot.name,
        strategy_type=bot.strategy_type,
        mode=bot.mode,
        status=bot.status,
        assigned_agent_id=bot.assigned_agent_id,
        config=config,
        latest_metrics=latest_metrics,
        created_at=bot.created_at,
        updated_at=bot.updated_at,
    )


def resolve_agent_url(agent_id: str, db: Session) -> str:
    """
    Return the base URL of an approved agent.

    :param agent_id: Unique identifier of the agent.
    :param db: Database session.
    :return: The agent's base URL string.
    :raises HTTPException: 404 if agent not found, 400 if not approved.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    if agent.approval_status != "approved":
        raise HTTPException(status_code=400, detail=_AGENT_NOT_APPROVED)  # NOSONAR - documented on calling routes
    return agent.base_url


def _find_running_agent_for_bot(bot_id: str, db: Session, include_stopped: bool = False) -> Agent | None:
    """Best-effort lookup: find an approved online agent currently hosting this bot.

    :param bot_id: Bot identifier to locate.
    :param db: Database session.
    :param include_stopped: When True, also match runners that exist but are not running.
    """
    agents = (
        db.query(Agent)
        .filter(Agent.status == "online", Agent.approval_status == "approved")
        .all()
    )
    for agent in agents:
        try:
            response = requests.get(
                f"{agent.base_url}/agent/bots",
                timeout=2,
                headers={"x-correlation-id": get_correlation_id()},
            )
        except requests.RequestException:
            continue
        if response.status_code >= 400:
            continue
        payload = response.json()
        if not isinstance(payload, list):
            continue
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("bot_id") == bot_id and (include_stopped or bool(item.get("running"))):
                return agent
    return None


def _dispatch_agent_command(
    agent: Agent,
    action: str,
    payload: dict,
    http_url: str,
    ws_timeout_seconds: float = 6.0,
) -> tuple[bool, str, dict | None]:
    """Send a command to an agent, preferring websocket and falling back to HTTP."""
    ws_ok, ws_message, ws_data = send_agent_command_ws(
        agent_id=agent.id,
        action=action,
        payload=payload,
        timeout_seconds=ws_timeout_seconds,
    )
    if ws_ok:
        return True, ws_message, ws_data

    ok, message = post_json(http_url, payload)
    if ok:
        return True, message, None
    if ws_message and ws_message not in {"", "ws_command_failed"}:
        return False, f"{message} (ws: {ws_message})", None
    return False, message, None


@router.post("/bots", responses={400: {"description": "Agent not approved"}, 404: {"description": "Agent not found"}})
def create_bot(payload: BotCreateRequest, db: DbSession) -> BotResponse:
    """
    Create a new bot with the given configuration (initially stopped).

    :param payload: Bot creation request with name and config.
    :param db: Database session (injected).
    :return: BotResponse for the newly created bot.
    """
    _validate_live_order_size(payload.config)
    assigned_agent_id = None
    if payload.assigned_agent_id:
        agent = db.query(Agent).filter(Agent.id == payload.assigned_agent_id).first()
        if not agent:
            raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
        if agent.approval_status != "approved" or agent.status != "online":
            raise HTTPException(status_code=400, detail="Assigned agent must be approved and online")
        assigned_agent_id = agent.id
    else:
        auto_agent = _select_least_loaded_agent(db)
        if auto_agent:
            assigned_agent_id = auto_agent.id

    bot_id = str(uuid.uuid4())
    starting_quote = float(payload.config.budget.quote_budget)
    starting_base = float(payload.config.budget.base_budget)
    initial_metrics = {
        "bot_id": bot_id,
        "timestamp": datetime.now(UTC).isoformat(),
        "runtime_seconds": 0,
        "price": 0.0,
        "quote_balance": starting_quote,
        "base_balance": starting_base,
        "base_value_in_quote": 0.0,
        "total_equity_quote": starting_quote,
        "realized_pnl_quote": 0.0,
        "unrealized_pnl_quote": 0.0,
        "skimmed_quote": 0.0,
        "trade_count": 0,
        "status": "stopped",
    }
    bot = Bot(
        id=bot_id,
        name=payload.name,
        strategy_type=payload.config.strategy,
        mode=payload.config.mode,
        status="stopped",
        assigned_agent_id=assigned_agent_id,
        config_json=payload.config.model_dump_json(),
        latest_metrics_json=json.dumps(initial_metrics),
        full_state_json=json.dumps({"snapshot": initial_metrics, "runner_state": None}),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot_to_response(bot)


@router.get("/bots")
def list_bots(db: DbSession) -> list[BotResponse]:
    """
    Return all bots with their current metrics.

    :param db: Database session (injected).
    :return: List of BotResponse models.
    """
    bots = db.query(Bot).all()
    return [bot_to_response(bot) for bot in bots]


@router.get("/bots/{bot_id}/logs", responses={400: {"description": "Bot cannot provide logs"}, 404: {"description": "Not found"}, 502: {"description": "Proxy failure"}})
def get_bot_logs(
    bot_id: str,
    db: DbSession,
    limit: int = 200,
    category: str | None = None,
) -> dict:
    """Proxy bot-specific logs from the bot's assigned agent.

    The manager forwards the request to ``/agent/logs`` with ``bot_id`` so
    only events for this bot are returned.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first() if bot.assigned_agent_id else None
    if not agent:
        inferred_agent = _find_running_agent_for_bot(bot.id, db)
        if inferred_agent:
            bot.assigned_agent_id = inferred_agent.id
            bot.updated_at = datetime.now(UTC)
            db.commit()
            agent = inferred_agent
    if not agent:
        raise HTTPException(status_code=400, detail="Bot is not assigned to an agent")
    if agent.approval_status != "approved":
        raise HTTPException(status_code=400, detail="Only approved agent logs are available")

    safe_limit = max(1, min(limit, 1000))
    query_params: dict[str, str | int] = {
        "limit": safe_limit,
        "bot_id": bot_id,
    }
    if category:
        query_params["category"] = category

    try:
        response = requests.get(
            f"{agent.base_url}/agent/logs",
            params=query_params,
            timeout=6,
            headers={"x-correlation-id": get_correlation_id()},
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch bot logs: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Agent returned {response.status_code}: {response.text}")

    payload = response.json()
    logs = payload.get("logs") if isinstance(payload, dict) else []
    if not isinstance(logs, list):
        logs = []

    return {
        "bot_id": bot_id,
        "agent_id": agent.id,
        "logs": logs,
    }


@router.post("/bots/{bot_id}/start", responses={400: {"description": "No agent available"}, 404: {"description": "Not found"}, 502: {"description": "Agent failure"}})
def start_bot(bot_id: str, payload: StartBotRequest, db: DbSession) -> dict:
    """
    Start a bot on a specific or auto-selected approved agent.

    :param bot_id: Unique identifier of the bot to start.
    :param payload: Request body with optional agent_id.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found, 400 if no agent available, 502 on agent failure.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    _validate_live_order_size(json.loads(bot.config_json))

    agent_id = payload.agent_id or bot.assigned_agent_id
    if not agent_id:
        agent = _select_least_loaded_agent(db)
        if not agent:
            raise HTTPException(status_code=400, detail="No approved online agent available")
        agent_id = agent.id

    agent_url = resolve_agent_url(agent_id, db)
    with scoped_context(bot_id=bot.id, agent_id=agent_id, component="manager.routes.bots.start"):
        trace_log(
            logger,
            "manager_start_bot_request",
            "Manager forwarding bot start request to agent",
            bot_id=bot.id,
            agent_id=agent_id,
            agent_url=agent_url,
        )
    logger.info("Starting bot %s on agent %s (%s)", bot.id, agent_id, agent_url)
    start_payload: dict = {
        "bot_id": bot.id,
        "config": json.loads(bot.config_json),
    }
    # Include saved runner state so the agent can resume from last position
    saved_state = _load_saved_runner_state(bot)
    if saved_state is not None:
        start_payload["runner_state"] = saved_state

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)

    ok, message, _ = _dispatch_agent_command(
        agent=agent,
        action="start_bot",
        payload=start_payload,
        http_url=f"{agent_url}/agent/bots/{bot.id}/start",
    )
    if not ok:
        with scoped_context(bot_id=bot.id, agent_id=agent_id, component="manager.routes.bots.start"):
            debug_log(
                logger,
                "manager_start_bot_failed",
                "Manager received failed bot-start response from agent",
                bot_id=bot.id,
                agent_id=agent_id,
                error=message,
            )
        logger.error("Agent %s failed to start bot %s: %s", agent_id, bot.id, message)
        raise HTTPException(status_code=502, detail=f"Agent start failed: {message}")

    bot.assigned_agent_id = agent_id
    bot.status = "initializing"
    _set_manual_stop_flag(bot, False)
    bot.updated_at = datetime.now(UTC)
    db.commit()
    with scoped_context(bot_id=bot.id, agent_id=agent_id, component="manager.routes.bots.start"):
        debug_log(
            logger,
            "manager_start_bot_ok",
            "Manager marked bot as initializing",
            bot_id=bot.id,
            agent_id=agent_id,
        )
    logger.info("Bot %s now initializing on agent %s", bot.id, agent_id)
    return {"ok": True}


@router.post("/bots/{bot_id}/move", responses={400: {"description": "Invalid target agent"}, 404: {"description": "Not found"}, 409: {"description": "Target agent full"}, 502: {"description": "Agent failure"}})
def move_bot(bot_id: str, payload: MoveBotRequest, db: DbSession) -> dict:
    """Manually move a bot to another approved online agent.

    If the bot is active, it is stopped on the source agent first,
    then started on the target agent with the current runner state.
    """
    from sqlalchemy import func

    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    target = db.query(Agent).filter(Agent.id == payload.agent_id).first()
    if not target:
        raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
    if target.approval_status != "approved" or target.status != "online":
        raise HTTPException(status_code=400, detail="Target agent must be approved and online")

    source_agent_id = bot.assigned_agent_id
    if source_agent_id == target.id:
        return {"ok": True, "message": "Bot is already assigned to this agent"}

    target_load = (
        db.query(func.count(Bot.id))
        .filter(Bot.assigned_agent_id == target.id, Bot.status.in_(_ACTIVE_BOT_STATUSES))
        .scalar()
        or 0
    )
    if target_load >= target.capacity:
        raise HTTPException(status_code=409, detail="Target agent is at capacity")

    was_active = bot.status in _ACTIVE_BOT_STATUSES
    if was_active and source_agent_id:
        source = db.query(Agent).filter(Agent.id == source_agent_id).first()
        if source and source.id != target.id:
            ok, message, _ = _dispatch_agent_command(
                agent=source,
                action="stop_bot",
                payload={"bot_id": bot.id},
                http_url=f"{source.base_url}/agent/bots/{bot.id}/stop",
            )
            if not ok:
                raise HTTPException(status_code=502, detail=f"Source agent stop failed: {message}")

    if was_active:
        start_payload: dict = {
            "bot_id": bot.id,
            "config": json.loads(bot.config_json),
        }
        saved_state = _load_saved_runner_state(bot)
        if saved_state:
            start_payload["runner_state"] = saved_state

        ok, message, _ = _dispatch_agent_command(
            agent=target,
            action="start_bot",
            payload=start_payload,
            http_url=f"{target.base_url}/agent/bots/{bot.id}/start",
        )
        if not ok:
            raise HTTPException(status_code=502, detail=f"Target agent start failed: {message}")
        bot.status = "initializing"

    bot.assigned_agent_id = target.id
    bot.updated_at = datetime.now(UTC)
    db.commit()

    if source_agent_id:
        add_agent_event(
            source_agent_id,
            "bot_moved_out",
            f"Bot {bot.name} ({bot.id}) was moved to agent {target.id}.",
        )
    add_agent_event(
        target.id,
        "bot_moved_in",
        f"Bot {bot.name} ({bot.id}) was manually assigned to this agent.",
    )
    logger.info("Bot %s moved from %s to %s", bot.id, source_agent_id, target.id)
    return {"ok": True}


@router.post("/bots/{bot_id}/stop", responses={404: {"description": "Not found"}})
def stop_bot(bot_id: str, db: DbSession) -> dict:
    """
    Stop a running bot and notify its assigned agent.

    If the agent is unreachable or missing, the bot is force-stopped
    and unassigned so it doesn't remain in a stuck "running" state.

    :param bot_id: Unique identifier of the bot to stop.
    :param db: Database session (injected).
    :return: Dict with ok status and optional warning.
    :raises HTTPException: 404 if bot not found.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    warning = None

    if bot.assigned_agent_id:
        agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first()
        if agent and agent.approval_status == "approved":
            ok, message, _ = _dispatch_agent_command(
                agent=agent,
                action="stop_bot",
                payload={"bot_id": bot.id},
                http_url=f"{agent.base_url}/agent/bots/{bot.id}/stop",
            )
            if not ok:
                logger.warning(
                    "Could not reach agent %s to stop bot %s: %s – force-stopping",
                    bot.assigned_agent_id, bot.id, message,
                )
                warning = f"Agent unreachable, bot force-stopped: {message}"
        else:
            warning = "Agent not found or not approved, bot force-stopped"

    bot.status = "stopped"
    bot.assigned_agent_id = None
    _set_manual_stop_flag(bot, True)
    bot.updated_at = datetime.now(UTC)
    db.commit()
    result: dict = {"ok": True}
    if warning:
        result["warning"] = warning
    return result


@router.delete("/bots/{bot_id}", responses={404: {"description": "Not found"}})
def delete_bot(bot_id: str, db: DbSession, payload: DeleteBotRequest | None = Body(default=None)) -> dict:
    """
    Delete a bot and its associated event data.

    Active bots are automatically stopped first.

    :param bot_id: Unique identifier of the bot to delete.
    :param db: Database session (injected).
    :return: Dict with ok status and optional stop warning.
    :raises HTTPException: 404 if bot not found.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    delete_mode = payload.delete_mode if payload else "delete_open_orders"

    stop_warning = None
    if delete_mode == "delete_open_orders":
        if bot.assigned_agent_id:
            agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first()
            if not agent or agent.approval_status != "approved":
                raise HTTPException(status_code=502, detail="Assigned agent unavailable for delete preparation")
            ok, message, _ = _dispatch_agent_command(
                agent=agent,
                action="prepare_delete",
                payload={"bot_id": bot.id, "delete_mode": delete_mode},
                http_url=f"{agent.base_url}/agent/bots/{bot.id}/prepare-delete",
            )
            if not ok:
                raise HTTPException(status_code=502, detail=f"Agent delete preparation failed: {message}")
            bot.status = "stopped"
            bot.assigned_agent_id = None
            _set_manual_stop_flag(bot, True)
            bot.updated_at = datetime.now(UTC)
            db.commit()
        else:
            inferred_agent = _find_running_agent_for_bot(bot.id, db, include_stopped=True)
            if inferred_agent:
                ok, message, _ = _dispatch_agent_command(
                    agent=inferred_agent,
                    action="prepare_delete",
                    payload={"bot_id": bot.id, "delete_mode": delete_mode},
                    http_url=f"{inferred_agent.base_url}/agent/bots/{bot.id}/prepare-delete",
                )
                if not ok:
                    raise HTTPException(status_code=502, detail=f"Agent delete preparation failed: {message}")

                # Preserve recovered routing for diagnostics until record removal.
                bot.assigned_agent_id = inferred_agent.id
                bot.status = "stopped"
                _set_manual_stop_flag(bot, True)
                bot.updated_at = datetime.now(UTC)
                db.commit()

            saved_state = _load_saved_runner_state(bot) or {}
            saved_open_orders = saved_state.get("open_orders") if isinstance(saved_state, dict) else {}
            has_saved_open_orders = isinstance(saved_open_orders, dict) and bool(saved_open_orders)
            if not inferred_agent and (bot.status in _ACTIVE_BOT_STATUSES or has_saved_open_orders):
                raise HTTPException(status_code=409, detail="Bot has no assigned agent to cancel open orders before delete")
    elif bot.status in _ACTIVE_BOT_STATUSES:
        if not bot.assigned_agent_id:
            raise HTTPException(status_code=409, detail="Bot has no assigned agent for delete preparation")
        agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first()
        if not agent or agent.approval_status != "approved":
            raise HTTPException(status_code=502, detail="Assigned agent unavailable for delete preparation")
        ok, message, _ = _dispatch_agent_command(
            agent=agent,
            action="prepare_delete",
            payload={"bot_id": bot.id, "delete_mode": delete_mode},
            http_url=f"{agent.base_url}/agent/bots/{bot.id}/prepare-delete",
        )
        if not ok:
            raise HTTPException(status_code=502, detail=f"Agent delete preparation failed: {message}")
        bot.status = "stopped"
        bot.assigned_agent_id = None
        _set_manual_stop_flag(bot, True)
        bot.updated_at = datetime.now(UTC)
        db.commit()

    # Clean up trade events and equity history
    delete_trade_events_for_bot(bot_id)
    from manager.app.events import EQUITY_HISTORY, EQUITY_HISTORY_LOCK
    with EQUITY_HISTORY_LOCK:
        EQUITY_HISTORY.pop(bot_id, None)
    db.delete(bot)
    db.commit()
    logger.info("Bot %s deleted", bot_id)
    result: dict = {"ok": True, "delete_mode": delete_mode}
    if stop_warning:
        result["warning"] = stop_warning
    return result


@router.post("/bots/{bot_id}/budget", responses={404: {"description": "Not found"}, 502: {"description": "Agent failure"}})
def update_budget(bot_id: str, payload: UpdateBudgetRequest, db: DbSession) -> dict:
    """
    Update the budget of a bot and forward the change to its agent if running.

    :param bot_id: Unique identifier of the bot.
    :param payload: Request body with quote_budget and base_budget.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found, 502 on agent failure.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    cfg = json.loads(bot.config_json)
    cfg_budget = cfg.get("budget", {})
    cfg_budget["quote_budget"] = payload.quote_budget
    cfg_budget["base_budget"] = payload.base_budget
    cfg["budget"] = cfg_budget
    bot.config_json = json.dumps(cfg)
    bot.updated_at = datetime.now(UTC)

    if bot.assigned_agent_id and bot.status in _ACTIVE_BOT_STATUSES:
        agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first()
        if not agent:
            raise HTTPException(status_code=404, detail=_AGENT_NOT_FOUND)
        budget_payload = {
            "bot_id": bot.id,
            "budget": {
                "quote_budget": payload.quote_budget,
                "base_budget": payload.base_budget,
            },
        }
        ok, message, _ = _dispatch_agent_command(
            agent=agent,
            action="update_budget",
            payload=budget_payload,
            http_url=f"{agent.base_url}/agent/bots/{bot.id}/budget",
        )
        if not ok:
            raise HTTPException(status_code=502, detail=f"Agent budget update failed: {message}")

    db.commit()
    return {"ok": True}


@router.post("/bots/{bot_id}/sync", responses={400: {"description": "Bot cannot sync"}, 404: {"description": "Not found"}, 502: {"description": "Agent failure"}})
def sync_bot(bot_id: str, db: DbSession) -> dict:
    """Force-sync a running bot with its exchange and refresh manager-side metrics."""
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first() if bot.assigned_agent_id else None
    if not agent:
        inferred_agent = _find_running_agent_for_bot(bot.id, db)
        if inferred_agent:
            bot.assigned_agent_id = inferred_agent.id
            bot.updated_at = datetime.now(UTC)
            db.commit()
            agent = inferred_agent

    if not agent:
        raise HTTPException(status_code=400, detail="Bot is not assigned to a running agent")
    if agent.approval_status != "approved":
        raise HTTPException(status_code=400, detail="Only approved agent sync is available")

    with scoped_context(bot_id=bot.id, agent_id=agent.id, component="manager.routes.bots.sync"):
        trace_log(
            logger,
            "manager_sync_bot_request",
            "Manager forwarding bot sync request to agent",
            bot_id=bot.id,
            agent_id=agent.id,
            agent_url=agent.base_url,
        )

    ok, message, details = _dispatch_agent_command(
        agent=agent,
        action="sync_bot",
        payload={"bot_id": bot.id},
        http_url=f"{agent.base_url}/agent/bots/{bot.id}/sync",
    )
    if not ok:
        with scoped_context(bot_id=bot.id, agent_id=agent.id, component="manager.routes.bots.sync"):
            debug_log(
                logger,
                "manager_sync_bot_failed",
                "Manager received failed bot-sync response from agent",
                bot_id=bot.id,
                agent_id=agent.id,
                error=message,
            )
        raise HTTPException(status_code=502, detail=f"Agent sync failed: {message}")

    bot.updated_at = datetime.now(UTC)
    db.commit()

    with scoped_context(bot_id=bot.id, agent_id=agent.id, component="manager.routes.bots.sync"):
        debug_log(
            logger,
            "manager_sync_bot_ok",
            "Manager triggered bot exchange sync successfully",
            bot_id=bot.id,
            agent_id=agent.id,
        )

    result: dict = {"ok": True, "bot_id": bot.id, "agent_id": agent.id, "message": message}
    if details:
        result["details"] = details
    return result


@router.post("/agents/{agent_id}/bots/{bot_id}/metrics", responses={404: {"description": "Bot not found"}})
def push_metrics(agent_id: str, bot_id: str, payload: MetricsPushRequest, db: DbSession) -> dict:
    return ingest_agent_metrics(agent_id=agent_id, bot_id=bot_id, payload=payload, db=db)


def ingest_agent_metrics(agent_id: str, bot_id: str, payload: MetricsPushRequest, db: Session) -> dict:
    """
    Ingest an agent metrics snapshot for a specific bot.

    Records the equity data-point for the budget trend chart and
    generates trade events when the trade count increases.

    :param agent_id: Unique identifier of the reporting agent.
    :param bot_id: Unique identifier of the bot.
    :param payload: Metrics data containing a BotSnapshot.
    :param db: Database session.
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    # Ignore stale snapshots from a bot that was manually stopped/unassigned.
    # Without this guard, a late "running" snapshot can resurrect the bot and
    # make failover logic reassign it automatically.
    state_flags = _load_bot_state_flags(bot)
    if bot.status == "stopped" and not bot.assigned_agent_id and bool(state_flags.get("manual_stop")):
        return {"ok": True, "ignored": "bot_manually_stopped"}

    # Keep assignment aligned with the reporting agent; this prevents temporary
    # unassignment from blocking bot-specific logs while the bot is still running.
    if bot.assigned_agent_id != agent_id:
        bot.assigned_agent_id = agent_id

    snapshot = payload.snapshot
    with scoped_context(bot_id=bot_id, agent_id=agent_id, component="manager.routes.bots.metrics"):
        trace_log(
            logger,
            "manager_metrics_ingest",
            "Manager received metrics snapshot",
            bot_id=bot_id,
            agent_id=agent_id,
            status=snapshot.status,
            trade_count=snapshot.trade_count,
            price=snapshot.price,
        )

    # ── Record equity history for budget trend chart ──
    add_equity_point(
        bot_id,
        snapshot.timestamp.isoformat(),
        snapshot.total_equity_quote,
        snapshot.price,
    )

    # ── Detect new trades and emit trade events ──
    config = json.loads(bot.config_json or "{}")
    market = config.get("market", "")
    if payload.trade_events:
        # Use detailed events from the agent
        for ev in payload.trade_events:
            add_trade_event(
                bot_id=bot_id,
                bot_name=bot.name,
                side=ev.get("side", "trade"),
                quote_amount=ev.get("quote_amount", 0),
                fill_count=ev.get("fill_count", 0),
                fee_paid_quote=ev.get("fee_paid_quote", 0.0),
                fee_rate=ev.get("fee_rate", 0.0),
                price=ev.get("price", snapshot.price),
                trade_pnl=ev.get("trade_pnl", 0),
                total_equity=ev.get("total_equity", snapshot.total_equity_quote),
                trade_number=ev.get("trade_number", snapshot.trade_count),
                event_type=ev.get("event_type", "trade"),
                level_index=ev.get("level_index"),
                market=market,
                order_id=ev.get("order_id"),
                exchange_order_id=ev.get("exchange_order_id"),
            )
            if ev.get("side") == "sell":
                matched_buy_base = float(ev.get("matched_buy_base", 0.0) or 0.0)
                sold_base = float(ev.get("base_amount", 0.0) or 0.0)
                base_remainder = float(ev.get("base_remainder_after_sell", 0.0) or 0.0)
                if matched_buy_base > 0 or sold_base > 0:
                    with scoped_context(bot_id=bot_id, agent_id=agent_id, component="manager.routes.bots.metrics"):
                        debug_log(
                            logger,
                            "manager_sell_base_reconcile",
                            "Manager received sell/base reconciliation details",
                            bot_id=bot_id,
                            agent_id=agent_id,
                            level_index=ev.get("level_index"),
                            matched_buy_base=round(matched_buy_base, 12),
                            sold_base=round(sold_base, 12),
                            base_remainder_after_sell=round(base_remainder, 12),
                        )
    else:
        # No fallback synthetic "trade" rows: they are not real orders and
        # create confusing PnL records in the database/UI.
        pass

    bot.status = snapshot.status
    bot.latest_metrics_json = snapshot.model_dump_json()
    _set_full_state(bot, snapshot, payload.runner_state)
    bot.updated_at = datetime.now(UTC)
    db.commit()
    with scoped_context(bot_id=bot_id, agent_id=agent_id, component="manager.routes.bots.metrics"):
        debug_log(
            logger,
            "manager_metrics_stored",
            "Manager stored metrics snapshot",
            bot_id=bot_id,
            agent_id=agent_id,
            trade_events=len(payload.trade_events or []),
        )
    return {"ok": True}


@router.get("/trade-events")
def list_trade_events(bot_id: str | None = None) -> list[dict]:
    """Return trade events from the database (most recent first)."""
    return get_trade_events(bot_id=bot_id)


@router.get("/trade-events/{event_id}")
def get_single_trade_event(event_id: str) -> dict:
    """Return a single trade event by ID, including linked order details."""
    from manager.app.database import SessionLocal
    from manager.app.models import TradeEvent as TE
    db = SessionLocal()
    try:
        ev = db.query(TE).filter(TE.id == event_id).first()
        if not ev:
            raise HTTPException(status_code=404, detail="Trade event not found")
        result = {
            "id": ev.id,
            "timestamp": ev.timestamp.isoformat() + "Z" if ev.timestamp else "",
            "bot_id": ev.bot_id,
            "bot_name": ev.bot_name,
            "market": ev.market or "",
            "event_type": ev.event_type,
            "order_id": ev.order_id,
            "exchange_order_id": ev.exchange_order_id,
            "side": ev.side,
            "quote_amount": ev.quote_amount,
            "fill_count": ev.fill_count,
            "fee_paid_quote": ev.fee_paid_quote,
            "fee_rate": ev.fee_rate,
            "price": ev.price,
            "trade_pnl": ev.trade_pnl,
            "total_equity": ev.total_equity,
            "trade_number": ev.trade_number,
            "level_index": ev.level_index,
            "linked_order_id": ev.linked_order_id,
            "linked_order": None,
            "pair_metrics": None,
        }
        if ev.linked_order_id:
            linked = db.query(TE).filter(TE.id == ev.linked_order_id).first()
            if linked:
                result["linked_order"] = {
                    "id": linked.id,
                    "order_id": linked.order_id,
                    "exchange_order_id": linked.exchange_order_id,
                    "timestamp": linked.timestamp.isoformat() + "Z" if linked.timestamp else "",
                    "event_type": linked.event_type,
                    "side": linked.side,
                    "quote_amount": linked.quote_amount,
                    "fill_count": linked.fill_count,
                    "fee_paid_quote": linked.fee_paid_quote,
                    "fee_rate": linked.fee_rate,
                    "price": linked.price,
                    "trade_pnl": linked.trade_pnl,
                    "level_index": linked.level_index,
                }
                bot = db.query(Bot).filter(Bot.id == ev.bot_id).first()
                if bot:
                    result["pair_metrics"] = _build_pair_metrics(bot, ev, linked)
        return result
    finally:
        db.close()


@router.get("/bots/{bot_id}/equity-history")
def get_equity_history(bot_id: str, db: DbSession, aggregation: str = "1m") -> dict:
    """Return equity data-points and budget info for the trend chart.

    :param bot_id: The bot to fetch history for.
    :param db: Database session (injected).
    :return: Dict with points list and budget metadata.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    config = json.loads(bot.config_json) if bot else {}
    budget = config.get("budget", {})
    starting_budget = budget.get("quote_budget", 0)
    metrics, _ = _normalized_metrics_for_bot(bot, db) if bot else ({}, 0.0)

    points = _get_bot_equity_points(bot, db) if bot else []
    agg = _normalize_equity_aggregation(aggregation)
    points = _aggregate_equity_points(points, agg)

    return {
        "points": points,
        "aggregation": agg,
        "starting_budget": starting_budget,
        "total_equity": metrics.get("total_equity_quote", starting_budget),
        "pnl": _trade_based_pnl(metrics, float(starting_budget or 0.0)),
    }


def _build_total_equity_series_entry(bot: Bot | None, db: Session, agg: str) -> tuple[dict | None, float, float, float]:
    if bot is None:
        return None, 0.0, 0.0, 0.0

    config = json.loads(bot.config_json or "{}")
    budget = config.get("budget", {})
    starting_budget = float(budget.get("quote_budget", 0) or 0.0)
    metrics, _ = _normalized_metrics_for_bot(bot, db)
    bot_total_equity = float(metrics.get("total_equity_quote", starting_budget) or starting_budget)
    bot_pnl = _trade_based_pnl(metrics, starting_budget)

    points = _aggregate_equity_points(_get_bot_equity_points(bot, db), agg)
    if points:
        last = dict(points[-1])
        last["v"] = bot_total_equity
        points[-1] = last

    if not points:
        return None, starting_budget, bot_total_equity, bot_pnl

    quote_currency = str(config.get("quote_currency", "") or budget.get("quote_currency", "") or "")
    return (
        {
            "bot_id": bot.id,
            "bot_name": bot.name,
            "quote_currency": quote_currency,
            "starting_budget": starting_budget,
            "total_equity": bot_total_equity,
            "pnl": bot_pnl,
            "points": points,
        },
        starting_budget,
        bot_total_equity,
        bot_pnl,
    )


def _build_total_equity_points(series: list[dict]) -> list[dict[str, float | str | int]]:
    ts_map: dict[str, float] = {}
    for entry in series:
        points = list(entry.get("points") or [])
        for point in points:
            t = str(point.get("t", ""))
            if not t:
                continue
            ts_map[t] = ts_map.get(t, 0.0) + float(point.get("v", 0.0) or 0.0)
    return [{"t": t, "v": v, "p": 0} for t, v in sorted(ts_map.items())]


@router.get("/bots/equity-history/total")
def get_total_equity_history(db: DbSession, aggregation: str = "1m") -> dict:
    """Return combined equity data-points across all bots.

    Besides the aggregated total line, this endpoint also returns one
    per-bot equity series so the frontend can render separate compound
    lines in "all bots" mode (including quote currency labels).
    """
    bots = db.query(Bot).all()
    total_starting_budget = 0.0
    total_equity = 0.0
    total_pnl = 0.0
    agg = _normalize_equity_aggregation(aggregation)
    series: list[dict] = []

    for bot in bots:
        if bot is None:
            continue
        entry, starting_budget, bot_total_equity, bot_pnl = _build_total_equity_series_entry(bot, db, agg)
        total_starting_budget += starting_budget
        total_equity += bot_total_equity
        total_pnl += bot_pnl
        if entry:
            series.append(entry)

    points = _build_total_equity_points(series)

    return {
        "points": points,
        "aggregation": agg,
        "starting_budget": total_starting_budget,
        "total_equity": total_equity,
        "pnl": total_pnl,
        "series": series,
    }


@router.get("/bots/{bot_id}/open-orders")
def get_open_orders(bot_id: str, db: DbSession) -> dict:
    """Proxy open orders from the agent running this bot."""
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail=_BOT_NOT_FOUND)

    if bot.assigned_agent_id:
        agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first()
        if agent:
            import requests as req
            try:
                resp = req.get(f"{agent.base_url}/agent/bots/{bot_id}/open-orders", timeout=5)
                resp.raise_for_status()
                return resp.json()
            except Exception:
                logger.debug("Falling back to saved open orders for bot %s", bot_id, exc_info=True)

    return _build_open_orders_from_saved_state(bot)
