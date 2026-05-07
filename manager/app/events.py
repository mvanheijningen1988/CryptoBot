"""In-memory event ring buffers.

Stores the most recent agent lifecycle events and trade events
for display in the dashboard notification panel.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from threading import Lock

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

# ── Trade events ──────────────────────────────────────────────

TRADE_EVENTS: list[dict] = []
TRADE_EVENTS_LOCK = Lock()
MAX_TRADE_EVENTS = 500


def add_trade_event(bot_id: str, bot_name: str, side: str, quote_amount: float,
                    price: float, trade_pnl: float, total_equity: float,
                    trade_number: int) -> None:
    """
    Record a trade execution event.

    :param bot_id: Bot that executed the trade.
    :param bot_name: Human-readable bot name.
    :param side: ``'buy'`` or ``'sell'``.
    :param quote_amount: Quote currency amount of the trade.
    :param price: Execution price.
    :param trade_pnl: Profit/loss of this individual trade.
    :param total_equity: Total equity after the trade.
    :param trade_number: Sequential trade number for this bot.
    """
    event = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "bot_id": bot_id,
        "bot_name": bot_name,
        "side": side,
        "quote_amount": round(quote_amount, 6),
        "price": round(price, 6),
        "trade_pnl": round(trade_pnl, 6),
        "total_equity": round(total_equity, 4),
        "trade_number": trade_number,
    }
    with TRADE_EVENTS_LOCK:
        TRADE_EVENTS.insert(0, event)
        if len(TRADE_EVENTS) > MAX_TRADE_EVENTS:
            del TRADE_EVENTS[MAX_TRADE_EVENTS:]

# ── Equity history ────────────────────────────────────────────

EQUITY_HISTORY: dict[str, list[dict]] = {}
EQUITY_HISTORY_LOCK = Lock()
MAX_EQUITY_POINTS = 500


def add_equity_point(bot_id: str, timestamp: str, total_equity: float) -> None:
    """
    Append an equity data-point for a bot's budget trend chart.

    :param bot_id: Bot identifier.
    :param timestamp: ISO timestamp string.
    :param total_equity: Total equity value at this point.
    """
    point = {"t": timestamp, "v": round(total_equity, 4)}
    with EQUITY_HISTORY_LOCK:
        if bot_id not in EQUITY_HISTORY:
            EQUITY_HISTORY[bot_id] = []
        EQUITY_HISTORY[bot_id].append(point)
        if len(EQUITY_HISTORY[bot_id]) > MAX_EQUITY_POINTS:
            EQUITY_HISTORY[bot_id] = EQUITY_HISTORY[bot_id][-MAX_EQUITY_POINTS:]
