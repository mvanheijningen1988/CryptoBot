"""Agent HTTP route handlers for bot lifecycle and log retrieval."""
from __future__ import annotations

import logging
import threading
import time as _time
from datetime import UTC, datetime

import requests

from fastapi import HTTPException
from fastapi.routing import APIRouter

from common.diagnostics import debug_log, scoped_context, trace_log
from common.exchange.bitvavo import BitvavoExchange
from agent.app.config import AGENT_ID, runner_manager
from agent.app.runtime_settings import get_exchange as get_runtime_exchange
from agent.app.runtime_settings import get_float as get_runtime_float
from agent.app.runtime_settings import get_setting as get_runtime_setting
from agent.app.schemas import BudgetPayload, DeleteBotPayload, StartBotPayload, StopBotPayload, SyncBotPayload
from agent.app.version import __version__

logger = logging.getLogger(__name__)

router = APIRouter()

_BALANCE_CACHE_LOCK = threading.Lock()
_BALANCE_ROWS_CACHE: dict[str, object] = {"ts": 0.0, "rows": []}

_TICKER_CACHE_LOCK = threading.Lock()
_TICKER_CACHE: dict[str, dict[str, float]] = {}


def _balance_cache_ttl_seconds() -> float:
    return max(0.0, float(get_runtime_float("AGENT_BALANCE_CACHE_TTL_SECONDS", 20.0)))


def _ticker_cache_ttl_seconds() -> float:
    return max(0.0, float(get_runtime_float("AGENT_TICKER_CACHE_TTL_SECONDS", 30.0)))


def _get_cached_balance_rows(now: float) -> list[dict] | None:
    with _BALANCE_CACHE_LOCK:
        ts = float(_BALANCE_ROWS_CACHE.get("ts", 0.0) or 0.0)
        rows = _BALANCE_ROWS_CACHE.get("rows")
        if now - ts <= _balance_cache_ttl_seconds() and isinstance(rows, list):
            return [dict(row) for row in rows if isinstance(row, dict)]
    return None


def _get_stale_balance_rows() -> list[dict] | None:
    with _BALANCE_CACHE_LOCK:
        rows = _BALANCE_ROWS_CACHE.get("rows")
        if isinstance(rows, list) and rows:
            return [dict(row) for row in rows if isinstance(row, dict)]
    return None


def _set_cached_balance_rows(now: float, rows: list[dict]) -> None:
    with _BALANCE_CACHE_LOCK:
        _BALANCE_ROWS_CACHE["ts"] = now
        _BALANCE_ROWS_CACHE["rows"] = [dict(row) for row in rows if isinstance(row, dict)]


def _to_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _iso_from_millis(value: object) -> str:
    ts = _to_float(value)
    if ts <= 0:
        return ""
    try:
        return datetime.fromtimestamp(ts / 1000.0, tz=UTC).isoformat().replace("+00:00", "Z")
    except Exception:
        return ""


def _fmt_order_type(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw == "market":
        return "Market"
    if raw == "stoploss":
        return "StopLoss"
    if raw == "stoplosslimit":
        return "StopLossLimit"
    return "Limit"


def _ticker_24h_for_market(market: str) -> dict:
    now = _time.time()
    with _TICKER_CACHE_LOCK:
        cached = _TICKER_CACHE.get(market)
        if isinstance(cached, dict):
            ts = float(cached.get("ts", 0.0) or 0.0)
            if now - ts <= _ticker_cache_ttl_seconds():
                return {
                    "last_price": _to_float(cached.get("last_price", 0.0)),
                    "change_pct": _to_float(cached.get("change_pct", 0.0)),
                }

    try:
        response = requests.get(
            "https://api.bitvavo.com/v2/ticker/24h",
            params={"market": market},
            timeout=4,
        )
    except requests.RequestException:
        return {"last_price": 0.0, "change_pct": 0.0}
    if response.status_code >= 400:
        return {"last_price": 0.0, "change_pct": 0.0}

    payload = response.json()
    data = payload[0] if isinstance(payload, list) and payload else payload if isinstance(payload, dict) else {}
    if not isinstance(data, dict):
        return {"last_price": 0.0, "change_pct": 0.0}

    open_price = _to_float(data.get("open"))
    last_price = _to_float(data.get("last"))
    change_pct = ((last_price - open_price) / open_price * 100.0) if open_price > 0 else 0.0
    result = {"last_price": last_price, "change_pct": change_pct}
    with _TICKER_CACHE_LOCK:
        _TICKER_CACHE[market] = {"ts": now, **result}
    return result


def _live_bitvavo_exchanges() -> list[BitvavoExchange]:
    exchanges: list[BitvavoExchange] = []
    for runner in runner_manager.runners.values():
        if not runner or not runner.running:
            continue
        if runner.config.mode != "live":
            continue
        if not isinstance(runner.exchange, BitvavoExchange):
            continue
        exchanges.append(runner.exchange)
    return exchanges


def _pick_exchange_for_market(market: str | None) -> BitvavoExchange | None:
    exchanges = _live_bitvavo_exchanges()
    if not exchanges:
        return None
    if market:
        target = market.strip().upper()
        for exchange in exchanges:
            if str(exchange.market or "").upper() == target:
                return exchange
    return exchanges[0]


def _split_market_pair(market: str) -> tuple[str, str]:
    raw = str(market or "").strip().upper()
    if "-" in raw:
        base, quote = raw.split("-", 1)
        if base and quote:
            return base, quote
    return "BTC", "EUR"


def _create_temporary_bitvavo_exchange(market: str | None) -> BitvavoExchange | None:
    exchange = get_runtime_exchange("bitvavo")
    api_key = str(exchange.get("endpoints_key", "") or "").strip()
    api_secret = str(exchange.get("secret", "") or "").strip()
    if not api_key or not api_secret:
        return None

    selected_market = str(market or get_runtime_setting("BITVAVO_DEFAULT_MARKET", "BTC-EUR")).strip().upper() or "BTC-EUR"
    base_currency, quote_currency = _split_market_pair(selected_market)

    exchange = BitvavoExchange(
        api_key=api_key,
        api_secret=api_secret,
        operator_id=0,
        bot_id=f"agent-notifications-{AGENT_ID}",
        market=selected_market,
        base_currency=base_currency,
        quote_currency=quote_currency,
        rest_url=str(exchange.get("base_url", "") or "").strip() or "https://api.bitvavo.com/v2",
    )
    try:
        exchange.start()
        return exchange
    except Exception as exc:
        runner_manager.log_system(
            "notifications_exchange_unavailable",
            "Failed to initialize temporary Bitvavo exchange for notifications.",
            {"market": selected_market, "error": str(exc)},
        )
        try:
            exchange.stop()
        except Exception:
            pass
        return None


def _acquire_exchange_for_market(market: str | None) -> tuple[BitvavoExchange | None, bool]:
    exchange = _pick_exchange_for_market(market)
    if exchange is not None:
        return exchange, False
    exchange = _create_temporary_bitvavo_exchange(market)
    return exchange, bool(exchange)


def _release_exchange(exchange: BitvavoExchange | None, temporary: bool) -> None:
    if not temporary or exchange is None:
        return
    try:
        exchange.stop()
    except Exception:
        pass


def _call_action_list(exchange: BitvavoExchange, action: str, payload: dict | None = None, timeout: float = 8.0) -> list[dict]:
    try:
        response = exchange._call_action(action, payload or {}, timeout=timeout)
    except Exception:
        return []

    if not isinstance(response, dict):
        return []
    if response.get("errorCode") is not None:
        return []

    data = response.get("response") if isinstance(response.get("response"), list) else response.get("orders")
    if action == "privateGetBalance":
        data = response.get("response") if isinstance(response.get("response"), list) else response.get("balances")
    if action == "privateGetTrades" and not isinstance(data, list):
        data = response.get("trades")
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _to_order_row(order: dict) -> dict:
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
        "operator_id": order.get("operatorId"),
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


def _is_open_status(value: object) -> bool:
    return str(value or "").strip().lower() in {"new", "awaitingtrigger", "partiallyfilled"}


@router.get("/health")
def health() -> dict:
    """
    Health check endpoint.

    :return: Dict with service status, name, version, and agent ID.
    """
    return {"status": "ok", "service": "agent", "version": __version__, "agent_id": AGENT_ID}


@router.post("/agent/bots/{bot_id}/start")
def start_bot(bot_id: str, payload: StartBotPayload) -> dict:
    """
    Start a trading bot with the given configuration.

    :param bot_id: Unique identifier of the bot to start.
    :param payload: Request body containing bot_id and config.
    :return: Acknowledgement dict.
    """
    with scoped_context(agent_id=AGENT_ID, bot_id=bot_id, component="agent.routes.start"):
        logger.info("START request for bot %s (mode=%s, market=%s)", bot_id, payload.config.mode, payload.config.market)
        trace_log(
            logger,
            "agent_start_request",
            "Agent received start request",
            bot_id=bot_id,
            mode=payload.config.mode,
            market=payload.config.market,
            agent_id=AGENT_ID,
        )
        try:
            runner_manager.start_bot(bot_id, payload.config, runner_state=payload.runner_state)
        except Exception as exc:
            logger.exception("Failed to start bot %s", bot_id)
            debug_log(logger, "agent_start_failed", "Agent failed to start bot", bot_id=bot_id, error=str(exc), agent_id=AGENT_ID)
            raise HTTPException(status_code=500, detail=f"Bot start failed: {exc}") from exc
        logger.info("Bot %s started successfully", bot_id)
        debug_log(logger, "agent_start_ok", "Agent started bot", bot_id=bot_id, agent_id=AGENT_ID)
        return {"ok": True}


@router.post("/agent/bots/{bot_id}/stop")
def stop_bot(bot_id: str, payload: StopBotPayload) -> dict:
    """
    Stop a running trading bot.

    :param bot_id: Unique identifier of the bot to stop.
    :param payload: Request body containing bot_id.
    :return: Acknowledgement dict.
    """
    with scoped_context(agent_id=AGENT_ID, bot_id=bot_id, component="agent.routes.stop"):
        logger.info("STOP request for bot %s", bot_id)
        try:
            runner_manager.stop_bot(bot_id)
        except Exception as exc:
            logger.exception("Failed to stop bot %s", bot_id)
            debug_log(logger, "agent_stop_failed", "Agent failed to stop bot", bot_id=bot_id, error=str(exc), agent_id=AGENT_ID)
            raise HTTPException(status_code=500, detail=f"Bot stop failed: {exc}") from exc
        logger.info("Bot %s stopped successfully", bot_id)
        debug_log(logger, "agent_stop_ok", "Agent stopped bot", bot_id=bot_id, agent_id=AGENT_ID)
        return {"ok": True}


@router.post("/agent/bots/{bot_id}/budget")
def update_budget(bot_id: str, payload: BudgetPayload) -> dict:
    """
    Update the budget for a running bot.

    :param bot_id: Unique identifier of the bot.
    :param payload: Request body containing bot_id and new budget.
    :return: Acknowledgement dict.
    """
    with scoped_context(agent_id=AGENT_ID, bot_id=bot_id, component="agent.routes.budget"):
        logger.info("BUDGET update for bot %s", bot_id)
        try:
            runner_manager.update_budget(bot_id, payload.budget)
        except Exception as exc:
            logger.exception("Failed to update budget for bot %s", bot_id)
            debug_log(logger, "agent_budget_failed", "Agent failed to update bot budget", bot_id=bot_id, error=str(exc), agent_id=AGENT_ID)
            raise HTTPException(status_code=500, detail=f"Budget update failed: {exc}") from exc
        debug_log(logger, "agent_budget_ok", "Agent updated bot budget", bot_id=bot_id, agent_id=AGENT_ID)
        return {"ok": True}


@router.post("/agent/bots/{bot_id}/sync")
def sync_bot(bot_id: str, payload: SyncBotPayload) -> dict:
    """Force a bot to sync state with exchange and immediately push a fresh snapshot."""
    with scoped_context(agent_id=AGENT_ID, bot_id=bot_id, component="agent.routes.sync"):
        logger.info("SYNC request for bot %s", bot_id)
        trace_log(
            logger,
            "agent_sync_request",
            "Agent received bot sync request",
            bot_id=bot_id,
            agent_id=AGENT_ID,
        )
        try:
            details = runner_manager.sync_bot(bot_id)
        except Exception as exc:
            logger.exception("Failed to sync bot %s", bot_id)
            debug_log(logger, "agent_sync_failed", "Agent failed to sync bot", bot_id=bot_id, error=str(exc), agent_id=AGENT_ID)
            raise HTTPException(status_code=500, detail=f"Bot sync failed: {exc}") from exc
        debug_log(logger, "agent_sync_ok", "Agent synced bot with exchange", bot_id=bot_id, agent_id=AGENT_ID, details=details)
        return {"ok": True, "details": details}


@router.get("/agent/bots")
def list_bots() -> list:
    """
    List all bots managed by this agent.

    :return: List of dicts with bot_id and running status.
    """
    return runner_manager.list_bots()


@router.get("/agent/logs")
def list_logs(limit: int = 200, bot_id: str | None = None, category: str | None = None) -> dict:
    """
    Return recent agent/bot log entries.

    :param limit: Maximum number of log entries to return (1-1000).
    :param bot_id: Optional filter by bot ID.
    :param category: Optional filter by log category.
    :return: Dict with agent info and filtered log entries.
    """
    safe_limit = max(1, min(limit, 1000))
    return {
        "agent_id": AGENT_ID,
        "logs": runner_manager.get_logs(limit=safe_limit, bot_id=bot_id, category=category),
    }


@router.get("/agent/bots/{bot_id}/open-orders")
def get_open_orders(bot_id: str) -> dict:
    """Return open grid orders for a running bot."""
    runner = runner_manager.runners.get(bot_id)
    if not runner or not runner.running:
        raise HTTPException(status_code=404, detail="Bot not running")

    if runner.config.mode == "live" and isinstance(runner.exchange, BitvavoExchange):
        orders = runner.exchange.list_open_grid_orders(
            level_prices=runner.strategy.levels,
            quote_amount=runner.config.grid.order_size_quote,
        )
        return {"bot_id": bot_id, "orders": orders}

    orders = runner.strategy.get_open_orders(runner.state)
    return {"bot_id": bot_id, "orders": orders}


@router.get("/agent/notifications/balance")
def notifications_balance() -> dict:
    now = _time.time()
    cached_rows = _get_cached_balance_rows(now)
    if cached_rows is not None:
        return {"rows": cached_rows}

    exchange, temporary = _acquire_exchange_for_market(None)
    if exchange is None:
        stale_rows = _get_stale_balance_rows()
        return {"rows": stale_rows or []}

    try:
        balances = _call_action_list(exchange, "privateGetBalance", {})
    finally:
        _release_exchange(exchange, temporary)

    rows: list[dict] = []
    for item in balances:
        asset = str(item.get("symbol", "") or "").upper()
        available = _to_float(item.get("available"))
        in_orders = _to_float(item.get("inOrder"))
        balance = available + in_orders
        if not asset or balance <= 0:
            continue

        if asset == "EUR":
            price = 1.0
            change_pct = 0.0
            euro_value = balance
        else:
            ticker = _ticker_24h_for_market(f"{asset}-EUR")
            price = _to_float(ticker.get("last_price"))
            change_pct = _to_float(ticker.get("change_pct"))
            euro_value = balance * price if price > 0 else 0.0

        rows.append(
            {
                "asset": asset,
                "price": round(price, 12),
                "change_24h": round(change_pct, 6),
                "euro_value": round(euro_value, 8),
                "balance": round(balance, 12),
                "available_balance": round(available, 12),
                "in_orders": round(in_orders, 12),
            }
        )

    rows.sort(key=lambda row: row.get("euro_value", 0.0), reverse=True)
    _set_cached_balance_rows(now, rows)
    return {"rows": rows}


@router.get("/agent/notifications/open-orders")
def notifications_open_orders(markets: str | None = None) -> dict:
    parsed_markets = [m.strip().upper() for m in str(markets or "").split(",") if m and m.strip()]
    orders: list[dict] = []

    if parsed_markets:
        for market in parsed_markets:
            exchange, temporary = _acquire_exchange_for_market(market)
            if exchange is None:
                continue
            try:
                market_orders = _call_action_list(exchange, "privateGetOrdersOpen", {"market": market})
            finally:
                _release_exchange(exchange, temporary)
            for order in market_orders:
                if str(order.get("market", "") or "").upper() == market:
                    orders.append(order)
    else:
        exchange, temporary = _acquire_exchange_for_market(None)
        if exchange is not None:
            try:
                orders = _call_action_list(exchange, "privateGetOrdersOpen", {})
            finally:
                _release_exchange(exchange, temporary)

    rows = [_to_order_row(order) for order in orders if _is_open_status(order.get("status"))]
    rows.sort(key=lambda row: row.get("date_time", ""), reverse=True)
    return {"rows": rows}


@router.get("/agent/notifications/order-history")
def notifications_order_history(markets: str | None = None, limit: int = 500) -> dict:
    parsed_markets = [m.strip().upper() for m in str(markets or "").split(",") if m and m.strip()]
    safe_limit = int(max(1, min(limit, 500)))
    orders: list[dict] = []

    if parsed_markets:
        for market in parsed_markets:
            exchange, temporary = _acquire_exchange_for_market(market)
            if exchange is None:
                continue
            try:
                orders.extend(_call_action_list(exchange, "privateGetOrders", {"market": market, "limit": safe_limit}))
            finally:
                _release_exchange(exchange, temporary)
    else:
        exchange, temporary = _acquire_exchange_for_market(None)
        if exchange is not None:
            try:
                orders = _call_action_list(exchange, "privateGetOrders", {"limit": safe_limit})
            finally:
                _release_exchange(exchange, temporary)

    rows = [_to_order_row(order) for order in orders]
    rows.sort(key=lambda row: row.get("date_time", ""), reverse=True)
    return {"rows": rows}


@router.get("/agent/notifications/trade-history")
def notifications_trade_history(markets: str | None = None, limit: int = 500) -> dict:
    parsed_markets = [m.strip().upper() for m in str(markets or "").split(",") if m and m.strip()]
    safe_limit = int(max(1, min(limit, 500)))
    trades: list[dict] = []

    if parsed_markets:
        for market in parsed_markets:
            exchange, temporary = _acquire_exchange_for_market(market)
            if exchange is None:
                continue
            try:
                trades.extend(_call_action_list(exchange, "privateGetTrades", {"market": market, "limit": safe_limit}))
            finally:
                _release_exchange(exchange, temporary)
    else:
        exchange, temporary = _acquire_exchange_for_market(None)
        if exchange is not None:
            try:
                trades = _call_action_list(exchange, "privateGetTrades", {"limit": safe_limit})
            finally:
                _release_exchange(exchange, temporary)

    rows: list[dict] = []
    for trade in trades:
        market = str(trade.get("market", "") or "")
        amount = _to_float(trade.get("amount"))
        price = _to_float(trade.get("price"))
        total = amount * price
        ts = _iso_from_millis(trade.get("timestamp") or trade.get("created") or trade.get("updated"))
        order_type = _fmt_order_type(trade.get("orderType"))
        side = str(trade.get("side", "") or "").lower()
        status = str(trade.get("status", "filled") or "filled")
        fee = _to_float(trade.get("fee") or trade.get("filledFee"))
        fee_currency = str(trade.get("feeCurrency", "") or trade.get("filledFeeCurrency", "") or "EUR")
        base_currency = market.split("-", 1)[0] if "-" in market else ""
        quote_currency = market.split("-", 1)[1] if "-" in market else "EUR"
        order_id = str(trade.get("orderId", "") or trade.get("id", "") or "")

        rows.append(
            {
                "id": str(trade.get("id", "") or order_id),
                "market": market,
                "side": side,
                "total": round(total, 8),
                "amount": amount,
                "price": price,
                "fee": fee,
                "date_time": ts,
                "order_type": order_type,
                "status": status,
                "filled_price": price,
                "filled_amount": amount,
                "transaction_fee": fee,
                "fee_currency": fee_currency,
                "quote_currency": quote_currency,
                "base_currency": base_currency,
                "date_created": ts,
                "date_updated": ts,
                "order_id": order_id,
            }
        )

    rows.sort(key=lambda row: row.get("date_time", ""), reverse=True)
    return {"rows": rows}


@router.post("/agent/bots/{bot_id}/prepare-delete")
def prepare_delete(bot_id: str, payload: DeleteBotPayload) -> dict:
    """Prepare a bot for deletion with the requested liquidation/cancel mode."""
    logger.info("PREPARE DELETE request for bot %s (mode=%s)", bot_id, payload.delete_mode)
    try:
        details = runner_manager.prepare_delete(bot_id, payload.delete_mode)
    except Exception as exc:
        logger.exception("Failed to prepare bot %s for delete", bot_id)
        raise HTTPException(status_code=500, detail=f"Delete prepare failed: {exc}") from exc
    logger.info("Bot %s prepared for delete", bot_id)
    return {"ok": True, "details": details}
