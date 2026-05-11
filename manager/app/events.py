"""Event helpers for agent lifecycle and trade event persistence.

Trade events are persisted to the database.  Agent events and equity
history remain in-memory ring buffers.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from threading import Lock
from threading import Condition

from manager.app.database import SessionLocal
from manager.app.models import TradeEvent
from sqlalchemy.orm import Session

# ── Agent lifecycle events ────────────────────────────────────

AGENT_EVENTS: list[dict] = []
AGENT_EVENTS_LOCK = Lock()
MAX_AGENT_EVENTS = 300

# ── Dashboard realtime updates (SSE broker) ───────────────────────

DASHBOARD_UPDATES_LOCK = Lock()
DASHBOARD_UPDATES_CONDITION = Condition(DASHBOARD_UPDATES_LOCK)
_dashboard_update_seq = 0
_dashboard_update_event = "init"
_dashboard_update_data: dict = {}


def publish_dashboard_update(event: str, data: dict | None = None) -> int:
    """Publish a lightweight dashboard update signal for SSE listeners."""
    global _dashboard_update_seq, _dashboard_update_event, _dashboard_update_data
    with DASHBOARD_UPDATES_CONDITION:
        _dashboard_update_seq += 1
        _dashboard_update_event = str(event or "update")
        _dashboard_update_data = data if isinstance(data, dict) else {}
        DASHBOARD_UPDATES_CONDITION.notify_all()
        return _dashboard_update_seq


def wait_for_dashboard_update(last_seq: int, timeout_seconds: float = 15.0) -> tuple[int, str, dict]:
    """Block until a newer dashboard update is available or timeout elapses."""
    with DASHBOARD_UPDATES_CONDITION:
        if _dashboard_update_seq <= int(last_seq):
            DASHBOARD_UPDATES_CONDITION.wait(timeout=max(0.0, float(timeout_seconds)))
        return _dashboard_update_seq, _dashboard_update_event, dict(_dashboard_update_data)


def add_agent_event(agent_id: str, event_type: str, message: str) -> None:
    """
    Append an agent lifecycle event to the in-memory event ring buffer.

    :param agent_id: Unique identifier of the agent.
    :param event_type: Event category (e.g. 'discovered', 'offline').
    :param message: Descriptive message for the event.
    """
    event = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "agent_id": agent_id,
        "event_type": event_type,
        "message": message,
    }
    with AGENT_EVENTS_LOCK:
        AGENT_EVENTS.insert(0, event)
        if len(AGENT_EVENTS) > MAX_AGENT_EVENTS:
            del AGENT_EVENTS[MAX_AGENT_EVENTS:]
    publish_dashboard_update("agent_event", {"agent_id": agent_id, "event_type": event_type})

# ── Trade events (persisted to DB) ───────────────────────────


def _is_valid_link_pair(source: TradeEvent, candidate: TradeEvent | None) -> bool:
    """Return whether two trade events form a valid opposite-side grid pair."""
    if candidate is None or source.id == candidate.id:
        return False

    source_side = str(source.side or "").lower()
    candidate_side = str(candidate.side or "").lower()
    source_type = str(source.event_type or "").lower()
    candidate_type = str(candidate.event_type or "").lower()
    if source_side not in {"buy", "sell"} or candidate_side not in {"buy", "sell"}:
        return False
    if source_side == candidate_side:
        return False
    if source.level_index is None or candidate.level_index is None:
        return False

    if source_side == "buy":
        return (
            source_type == "order_filled"
            and candidate_side == "sell"
            and candidate.level_index == source.level_index + 1
            and candidate_type in {"order_placed", "order_filled"}
        )

    return (
        source_type in {"order_placed", "order_filled"}
        and source.level_index > 0
        and candidate_side == "buy"
        and candidate.level_index == source.level_index - 1
        and candidate_type == "order_filled"
    )


def _find_link_candidate(db: Session, bot_id: str, row: TradeEvent) -> TradeEvent | None:
    """Find the best opposite-side link candidate for one trade event."""
    def _as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    side = str(row.side or "").lower()
    event_type = str(row.event_type or "").lower()
    level_index = row.level_index
    if level_index is None:
        return None

    if side == "buy" and event_type == "order_filled":
        target_side = "sell"
        target_level = level_index + 1
        target_types = ["order_placed", "order_filled"]
    elif side == "sell" and event_type in {"order_placed", "order_filled"} and level_index > 0:
        target_side = "buy"
        target_level = level_index - 1
        target_types = ["order_filled"]
    else:
        return None

    candidates = (
        db.query(TradeEvent)
        .filter(
            TradeEvent.bot_id == bot_id,
            TradeEvent.id != row.id,
            TradeEvent.side == target_side,
            TradeEvent.level_index == target_level,
            TradeEvent.event_type.in_(target_types),
        )
        .order_by(TradeEvent.timestamp.desc())
        .all()
    )
    for candidate in candidates:
        candidate_ts = _as_utc(candidate.timestamp)
        row_ts = _as_utc(row.timestamp)
        if side == "sell" and candidate_ts and row_ts and candidate_ts > row_ts:
            continue
        if candidate.linked_order_id not in {None, row.id} and _is_valid_link_pair(candidate, db.query(TradeEvent).filter(TradeEvent.id == candidate.linked_order_id).first()):
            continue
        if _is_valid_link_pair(row, candidate):
            return candidate
    return None


def add_trade_event(bot_id: str, bot_name: str, side: str, quote_amount: float,
                    price: float, trade_pnl: float, total_equity: float,
                    trade_number: int, event_type: str = "trade",
                    level_index: int | None = None,
                    market: str = "",
                    order_id: str | None = None,
                    exchange_order_id: str | None = None,
                    fill_count: int = 0,
                    fee_paid_quote: float = 0.0,
                    fee_rate: float = 0.0) -> str:
    """Persist a trade-related event to the database. Returns the event ID.

    Buy/sell events are linked per grid step:
    * buy fill at level ``N`` links to sell at level ``N + 1``
    * sell event at level ``N`` links to buy fill at level ``N - 1``

    This keeps order details navigable even when events are persisted
    in a different order (e.g. sell placement logged before buy fill).
    """
    event_id = str(uuid.uuid4())
    db = SessionLocal()
    try:
        row = None
        if order_id:
            row = (
                db.query(TradeEvent)
                .filter(TradeEvent.bot_id == bot_id, TradeEvent.order_id == order_id)
                .first()
            )

        # Recovery/re-sync can change local order IDs (e.g. existing-*/reconciled-*).
        # In that case, promote the latest still-open placed row on the same
        # side+level to filled/cancelled instead of creating duplicates.
        if (
            row is None
            and event_type in {"order_filled", "order_cancelled"}
            and level_index is not None
        ):
            row = (
                db.query(TradeEvent)
                .filter(
                    TradeEvent.bot_id == bot_id,
                    TradeEvent.event_type == "order_placed",
                    TradeEvent.side == side,
                    TradeEvent.level_index == level_index,
                )
                .order_by(TradeEvent.timestamp.desc())
                .first()
            )

        if row is None:
            row = TradeEvent(id=event_id, bot_id=bot_id, bot_name=bot_name)
            db.add(row)
        else:
            event_id = row.id

        row.order_id = order_id
        if exchange_order_id:
            row.exchange_order_id = exchange_order_id
        row.bot_name = bot_name
        row.timestamp = datetime.now(UTC)
        row.event_type = event_type
        row.side = side
        row.quote_amount = quote_amount
        row.fill_count = int(fill_count or 0)
        row.fee_paid_quote = fee_paid_quote
        row.fee_rate = fee_rate
        row.price = price
        row.trade_pnl = trade_pnl
        row.total_equity = total_equity
        row.trade_number = trade_number
        row.level_index = level_index
        row.market = market

        existing_link = None
        if row.linked_order_id:
            existing_link = db.query(TradeEvent).filter(TradeEvent.id == row.linked_order_id).first()
            if not _is_valid_link_pair(row, existing_link):
                row.linked_order_id = None
                existing_link = None

        candidate = _find_link_candidate(db, bot_id, row)
        if candidate is not None:
            linked_order_id = candidate.id
            row.linked_order_id = candidate.id
            existing_back_link = None
            if candidate.linked_order_id:
                existing_back_link = db.query(TradeEvent).filter(TradeEvent.id == candidate.linked_order_id).first()
            if not _is_valid_link_pair(candidate, existing_back_link) or candidate.linked_order_id in {None, row.id}:
                candidate.linked_order_id = row.id
        elif existing_link is None:
            row.linked_order_id = None

        db.commit()
    finally:
        db.close()
    publish_dashboard_update("trade_event", {"bot_id": bot_id, "event_type": event_type, "side": side})
    return event_id


def get_trade_events(bot_id: str | None = None, limit: int = 200) -> list[dict]:
    """Fetch recent trade events from the database."""
    db = SessionLocal()
    try:
        q = db.query(TradeEvent).order_by(TradeEvent.timestamp.desc())
        if bot_id:
            q = q.filter(TradeEvent.bot_id == bot_id)
        rows = q.limit(limit).all()
        # Remove stale placed rows that were superseded by newer terminal events
        # after recovery/re-sync changed local order IDs.
        terminal_keys: set[tuple[str, str, int]] = set()
        filtered_rows: list[TradeEvent] = []
        for r in rows:
            side = str(r.side or "").lower()
            if r.level_index is not None and side in {"buy", "sell"}:
                key = (r.bot_id, side, int(r.level_index))
                if r.event_type in {"order_filled", "order_cancelled"}:
                    terminal_keys.add(key)
                elif r.event_type == "order_placed" and key in terminal_keys:
                    continue
            filtered_rows.append(r)

        return [
            {
                "id": r.id,
                "order_id": r.order_id,
                "exchange_order_id": r.exchange_order_id,
                "timestamp": r.timestamp.isoformat() + "Z" if r.timestamp else "",
                "bot_id": r.bot_id,
                "bot_name": r.bot_name,
                "market": r.market or "",
                "event_type": r.event_type,
                "side": r.side,
                "quote_amount": r.quote_amount,
                "fill_count": r.fill_count,
                "fee_paid_quote": r.fee_paid_quote,
                "fee_rate": r.fee_rate,
                "price": r.price,
                "trade_pnl": r.trade_pnl,
                "total_equity": r.total_equity,
                "trade_number": r.trade_number,
                "level_index": r.level_index,
                "linked_order_id": r.linked_order_id,
            }
            for r in filtered_rows
        ]
    finally:
        db.close()


def delete_trade_events_for_bot(bot_id: str) -> int:
    """Delete all trade events for a bot. Returns count deleted."""
    db = SessionLocal()
    try:
        count = db.query(TradeEvent).filter(TradeEvent.bot_id == bot_id).delete()
        db.commit()
        return count
    finally:
        db.close()

# ── Equity history ────────────────────────────────────────────

EQUITY_HISTORY: dict[str, list[dict]] = {}
EQUITY_HISTORY_LOCK = Lock()
MAX_EQUITY_POINTS = 500


def add_equity_point(bot_id: str, timestamp: str, total_equity: float, price: float = 0.0) -> None:
    """
    Append an equity data-point for a bot's budget trend chart.

    :param bot_id: Bot identifier.
    :param timestamp: ISO timestamp string.
    :param total_equity: Total equity value at this point.
    :param price: Market price at this point.
    """
    point = {"t": timestamp, "v": total_equity, "p": price}
    with EQUITY_HISTORY_LOCK:
        if bot_id not in EQUITY_HISTORY:
            EQUITY_HISTORY[bot_id] = []
        EQUITY_HISTORY[bot_id].append(point)
        if len(EQUITY_HISTORY[bot_id]) > MAX_EQUITY_POINTS:
            EQUITY_HISTORY[bot_id] = EQUITY_HISTORY[bot_id][-MAX_EQUITY_POINTS:]
    # Equity updates are emitted as lightweight wake-up signals.
    publish_dashboard_update("equity_point", {"bot_id": bot_id})
