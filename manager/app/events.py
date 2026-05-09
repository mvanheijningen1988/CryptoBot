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


def add_trade_event(bot_id: str, bot_name: str, side: str, quote_amount: float,
                    price: float, trade_pnl: float, total_equity: float,
                    trade_number: int, event_type: str = "trade",
                    level_index: int | None = None,
                    market: str = "",
                    order_id: str | None = None,
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
    linked_order_id = None

    db = SessionLocal()
    try:
        row = None
        if order_id:
            row = (
                db.query(TradeEvent)
                .filter(TradeEvent.bot_id == bot_id, TradeEvent.order_id == order_id)
                .first()
            )

        if row is None:
            row = TradeEvent(id=event_id, bot_id=bot_id, bot_name=bot_name)
            db.add(row)
        else:
            event_id = row.id

        row.order_id = order_id
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

        # Link sell events to the originating confirmed buy fill (N-1).
        if side == "sell" and level_index is not None and level_index > 0:
            linked_buy_level = level_index - 1
            buy = (
                db.query(TradeEvent)
                .filter(
                    TradeEvent.bot_id == bot_id,
                    TradeEvent.event_type == "order_filled",
                    TradeEvent.side == "buy",
                    TradeEvent.level_index == linked_buy_level,
                    TradeEvent.linked_order_id.is_(None),
                )
                .order_by(TradeEvent.timestamp.desc())
                .first()
            )
            if buy:
                linked_order_id = buy.id
                if not buy.linked_order_id:
                    buy.linked_order_id = event_id

        # Link buy fills to the sell event one level above (N+1), even if
        # that sell placement was persisted before this buy fill.
        if event_type == "order_filled" and side == "buy" and level_index is not None:
            linked_sell_level = level_index + 1
            sell = (
                db.query(TradeEvent)
                .filter(
                    TradeEvent.bot_id == bot_id,
                    TradeEvent.side == "sell",
                    TradeEvent.level_index == linked_sell_level,
                    TradeEvent.event_type.in_(["order_placed", "order_filled"]),
                    TradeEvent.linked_order_id.is_(None),
                )
                .order_by(TradeEvent.timestamp.desc())
                .first()
            )
            if sell:
                linked_order_id = sell.id
                if not sell.linked_order_id:
                    sell.linked_order_id = event_id

        if linked_order_id is not None:
            row.linked_order_id = linked_order_id

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
        return [
            {
                "id": r.id,
                "order_id": r.order_id,
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
    # Equity updates are emitted as lightweight wake-up signals.
    publish_dashboard_update("equity_point", {"bot_id": bot_id})
