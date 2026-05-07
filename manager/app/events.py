"""Event helpers for agent lifecycle and trade event persistence.

Trade events are persisted to the database.  Agent events and equity
history remain in-memory ring buffers.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from threading import Lock

from manager.app.database import SessionLocal
from manager.app.models import TradeEvent

# ── Agent lifecycle events ────────────────────────────────────

AGENT_EVENTS: list[dict] = []
AGENT_EVENTS_LOCK = Lock()
MAX_AGENT_EVENTS = 300


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

# ── Trade events (persisted to DB) ───────────────────────────


def add_trade_event(bot_id: str, bot_name: str, side: str, quote_amount: float,
                    price: float, trade_pnl: float, total_equity: float,
                    trade_number: int, event_type: str = "trade",
                    level_index: int | None = None,
                    market: str = "") -> str:
    """Persist a trade-related event to the database. Returns the event ID.

    For order_filled sell events, automatically links to the most recent
    order_filled buy at the same level for the same bot.
    """
    event_id = str(uuid.uuid4())
    linked_order_id = None

    # Link sell fills to their buy counterpart at the same level
    if event_type == "order_filled" and side == "sell" and level_index is not None:
        db = SessionLocal()
        try:
            buy = (
                db.query(TradeEvent)
                .filter(
                    TradeEvent.bot_id == bot_id,
                    TradeEvent.event_type == "order_filled",
                    TradeEvent.side == "buy",
                    TradeEvent.level_index == level_index,
                )
                .order_by(TradeEvent.timestamp.desc())
                .first()
            )
            if buy:
                linked_order_id = buy.id
                # Back-link the buy to this sell
                buy.linked_order_id = event_id
                db.commit()
        finally:
            db.close()

    row = TradeEvent(
        id=event_id,
        bot_id=bot_id,
        bot_name=bot_name,
        timestamp=datetime.now(UTC),
        event_type=event_type,
        side=side,
        quote_amount=quote_amount,
        price=price,
        trade_pnl=trade_pnl,
        total_equity=total_equity,
        trade_number=trade_number,
        level_index=level_index,
        market=market,
        linked_order_id=linked_order_id,
    )
    db = SessionLocal()
    try:
        db.add(row)
        db.commit()
    finally:
        db.close()
    return event_id


def get_trade_events(bot_id: str | None = None, limit: int = 200) -> list[dict]:
    """Fetch recent trade events from the database."""
    db = SessionLocal()
    try:
        q = db.query(TradeEvent).order_by(TradeEvent.timestamp.desc())
        if bot_id:
            q = q.filter(TradeEvent.bot_id == bot_id)
        rows = q.limit(limit).all()
        return [
            {
                "id": r.id,
                "timestamp": r.timestamp.isoformat() + "Z" if r.timestamp else "",
                "bot_id": r.bot_id,
                "bot_name": r.bot_name,
                "market": r.market or "",
                "event_type": r.event_type,
                "side": r.side,
                "quote_amount": r.quote_amount,
                "price": r.price,
                "trade_pnl": r.trade_pnl,
                "total_equity": r.total_equity,
                "trade_number": r.trade_number,
                "level_index": r.level_index,
                "linked_order_id": r.linked_order_id,
            }
            for r in rows
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
