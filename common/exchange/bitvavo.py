"""Live Bitvavo exchange adapter using websocket API v2.

Authenticates via HMAC-SHA256, subscribes to ticker events for real-time
prices, and executes market orders through the websocket action API.
"""
from __future__ import annotations

import logging
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any

import requests
import websocket

from common.exchange.base import Exchange
from common.models import TradeSignal


logger = logging.getLogger(__name__)


class BitvavoExchange(Exchange):
    """Websocket-based Bitvavo exchange adapter.

    Maintains a persistent websocket connection, a background reader
    thread, and synchronous request/response helpers for order execution
    and balance queries.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        market: str,
        base_currency: str,
        quote_currency: str,
        operator_id: int | None = None,
        ws_url: str = "wss://ws.bitvavo.com/v2/",
    ) -> None:
        """
        Create an adapter for a market using the given API credentials.

        :param api_key: Bitvavo API key.
        :param api_secret: Bitvavo API secret.
        :param market: Trading pair symbol (e.g. 'BTC-EUR').
        :param base_currency: Base currency code (e.g. 'BTC').
        :param quote_currency: Quote currency code (e.g. 'EUR').
        :param ws_url: Bitvavo websocket endpoint URL.
        """
        self.api_key = api_key
        self.api_secret = api_secret
        self.operator_id = operator_id
        self.market = market
        self.base_currency = base_currency
        self.quote_currency = quote_currency
        self.ws_url = ws_url
        self.rest_url = "https://api.bitvavo.com/v2"

        self.ws: websocket.WebSocket | None = None
        self.reader_thread: threading.Thread | None = None
        self.running = False
        self.authenticated = False
        self.request_id = 0
        self.lock = threading.Lock()

        self.latest_price: float | None = None
        self.quote_balance: float = 0.0
        self.base_balance: float = 0.0

        self.pending_events: dict[int, threading.Event] = {}
        self.pending_responses: dict[int, dict[str, Any]] = {}
        self.price_update_event = threading.Event()
        self._market_activity_event = threading.Event()

        # Limit order tracking
        self._limit_orders: dict[str, dict[str, Any]] = {}  # order_id → order info
        self._exchange_order_map: dict[str, str] = {}  # exchange_order_id → our order_id
        self._fills: list[dict[str, Any]] = []
        self._fills_lock = threading.Lock()
        self._not_authenticated_error = "Bitvavo websocket is not authenticated"
        self._price_decimals = 6
        self._amount_decimals = 6
        self._quote_decimals = 6
        self._last_sync_matches: list[dict[str, Any]] = []
        self._open_order_refresh_interval_seconds = float(
            os.getenv("BITVAVO_OPEN_ORDERS_RECONCILE_SECONDS", "5")
        )
        self._last_open_order_refresh_at = 0.0
        self._planned_level_reconcile_interval_seconds = float(
            os.getenv("BITVAVO_PLANNED_LEVEL_RECONCILE_SECONDS", "15")
        )
        self._last_planned_level_reconcile_at = 0.0
        self._processed_exchange_order_ids: set[str] = set()
        self._action_send_retry_attempts = max(
            1,
            int(os.getenv("BITVAVO_ACTION_RETRY_ATTEMPTS", "3") or 3),
        )
        self._reconnect_backoff_seconds = max(
            0.0,
            float(os.getenv("BITVAVO_RECONNECT_BACKOFF_SECONDS", "0.25") or 0.25),
        )
        self._reconnect_lock = threading.Lock()
        self._stop_requested = False

    def _is_transient_transport_error(self, exc: Exception) -> bool:
        """Return whether an action transport exception is retryable."""
        if isinstance(exc, TimeoutError):
            return True
        if isinstance(exc, (websocket.WebSocketException, OSError, ConnectionError)):
            return True
        message = str(exc).lower()
        return any(
            token in message
            for token in (
                "bad_length",
                "bad length",
                "broken pipe",
                "connection reset",
                "connection aborted",
                "eof",
                "socket is already closed",
                "websocket is not connected",
                "timed out",
            )
        )

    def _close_socket(self) -> None:
        """Close websocket transport and mark auth disconnected."""
        self.authenticated = False
        self.running = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None

    def _connect_and_authenticate(self) -> None:
        """Open websocket, authenticate and subscribe required channels."""
        self.ws = websocket.create_connection(self.ws_url, timeout=10)
        self.running = True

        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()

        timestamp_ms = int(time.time() * 1000)
        signature = self._create_signature(timestamp_ms)
        auth_payload = {
            "key": self.api_key,
            "signature": signature,
            "timestamp": timestamp_ms,
        }
        auth_response = self._call_action("authenticate", auth_payload, timeout=10.0, retry_transport=False)
        if auth_response.get("errorCode") is not None:
            self._close_socket()
            raise self._raise_action_error(
                "authenticate",
                auth_payload,
                f"Bitvavo authenticate failed: {auth_response.get('error') or auth_response.get('errorCode')}",
            )

        self._send_json(
            {
                "action": "subscribe",
                "channels": [
                    {"name": "ticker", "markets": [self.market]},
                    {"name": "account", "markets": [self.market]},
                ],
            }
        )
        self.authenticated = True

    def _reconnect_transport(self, reason: str) -> None:
        """Rebuild websocket transport and re-authenticate after transient errors."""
        if self._stop_requested:
            raise RuntimeError("Bitvavo websocket reconnect skipped: exchange is stopping")

        with self._reconnect_lock:
            if self._stop_requested:
                raise RuntimeError("Bitvavo websocket reconnect skipped: exchange is stopping")
            logger.warning("Bitvavo websocket reconnecting after transport failure: %s", reason)
            self._close_socket()
            if self._reconnect_backoff_seconds > 0:
                time.sleep(self._reconnect_backoff_seconds)
            self._connect_and_authenticate()

    def _extract_action_response(self, response: dict[str, Any]) -> dict[str, Any]:
        """Return the response body for websocket action replies."""
        body = response.get("response")
        return body if isinstance(body, dict) else response

    def _request_context(self, action: str, body: dict[str, Any]) -> str:
        """Format action + payload for exchange error messages."""
        try:
            payload = json.dumps(body, sort_keys=True)
        except TypeError:
            payload = str(body)
        return f"request_action={action}, request_payload={payload}"

    def _raise_action_error(self, action: str, body: dict[str, Any], message: str) -> RuntimeError:
        """Create a RuntimeError enriched with original request details."""
        return RuntimeError(f"{message} | {self._request_context(action, body)}")

    def _to_float(self, value: Any) -> float:
        """Best-effort float parsing helper."""
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _format_decimal(self, value: Any, decimals: int) -> str:
        """Format a number to exactly ``decimals`` places (max 6), rounded down."""
        safe_decimals = max(0, min(int(decimals), 6))
        quantum = "0" if safe_decimals == 0 else "0." + ("0" * safe_decimals)
        try:
            quantized = Decimal(str(value)).quantize(Decimal(quantum), rounding=ROUND_DOWN)
            return format(quantized, "f")
        except (InvalidOperation, TypeError, ValueError):
            return format(Decimal("0").quantize(Decimal(quantum)), "f")

    def _format_price(self, value: Any) -> str:
        """Format order price with market precision."""
        return self._format_decimal(value, self._price_decimals)

    def _format_amount_base(self, value: Any) -> str:
        """Format base amount with market precision."""
        return self._format_decimal(value, self._amount_decimals)

    def _format_amount_quote(self, value: Any) -> str:
        """Format quote amount with market precision."""
        return self._format_decimal(value, self._quote_decimals)

    def _read_precision(self, item: dict[str, Any], key: str, fallback: int) -> int:
        """Read one precision field from market metadata and clamp to [0, 6]."""
        raw = item.get(key)
        try:
            return max(0, min(int(raw), 6))
        except (TypeError, ValueError):
            return fallback

    def _infer_precision_from_min_value(self, value: Any, fallback: int) -> int:
        """Infer decimals from values like minOrderInBaseAsset/minOrderInQuoteAsset."""
        if value in (None, ""):
            return fallback
        text = str(value)
        if "." not in text:
            return 0
        fraction = text.split(".", 1)[1].rstrip("0")
        if not fraction:
            return 0
        return max(0, min(len(fraction), 6))

    def _load_market_precision(self) -> None:
        """Load market-specific precision from Bitvavo REST markets endpoint."""
        try:
            response = requests.get(
                f"{self.rest_url}/markets",
                params={"market": self.market},
                timeout=6,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return

        item: dict[str, Any] | None = None
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            item = payload[0]
        elif isinstance(payload, dict):
            item = payload
        if not item:
            return

        self._price_decimals = self._read_precision(item, "pricePrecision", self._price_decimals)
        self._amount_decimals = self._read_precision(item, "amountPrecision", self._amount_decimals)
        self._quote_decimals = self._read_precision(
            item,
            "quotePrecision",
            self._infer_precision_from_min_value(item.get("minOrderInQuoteAsset"), self._quote_decimals),
        )
        if "amountPrecision" not in item:
            self._amount_decimals = self._infer_precision_from_min_value(
                item.get("minOrderInBaseAsset"),
                self._amount_decimals,
            )

    def _fee_to_quote(self, fee_amount: Any, fee_currency: Any, fill_price: float) -> float:
        """Convert a fee amount to quote currency when needed."""
        fee_paid = self._to_float(fee_amount)
        if fee_paid == 0:
            return 0.0

        currency = str(fee_currency or "").upper()
        if currency == self.quote_currency.upper():
            return fee_paid
        if currency == self.base_currency.upper():
            return fee_paid * fill_price
        return fee_paid

    def _remove_tracked_order(self, order_id: str) -> None:
        """Drop a locally tracked order and its exchange mapping."""
        order_info = self._limit_orders.pop(order_id, None)
        if not order_info:
            return
        exchange_oid = order_info.get("exchange_order_id", "")
        if exchange_oid:
            self._exchange_order_map.pop(exchange_oid, None)

    def _grid_level_reference(self, level_index: int | None) -> str | None:
        """Return the stable logical reference for one open grid level."""
        if level_index is None:
            return None
        return f"level-{int(level_index)}"

    def _client_order_id(self, order_id: str, client_reference: str | None = None) -> str:
        """Return a Bitvavo-compatible UUID for one logical open order."""
        identity = client_reference or order_id
        operator_part = str(self.operator_id) if self.operator_id is not None else "no-operator"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cryptobot:{self.market}:{operator_part}:{identity}"))

    def _build_fill_from_order_status(self, order_id: str, order_info: dict[str, Any], order_state: dict[str, Any]) -> dict[str, Any]:
        """Create the persisted fill payload from Bitvavo order details."""
        fills = order_state.get("fills") if isinstance(order_state.get("fills"), list) else []
        fill_count = len([fill for fill in fills if isinstance(fill, dict)])
        status = str(order_state.get("status", "") or "")
        total_base = 0.0
        total_quote = 0.0
        fee_from_fills_quote = 0.0

        for fill in fills:
            if not isinstance(fill, dict):
                continue
            amount = self._to_float(fill.get("amount"))
            price = self._to_float(fill.get("price"))
            total_base += amount
            total_quote += amount * price
            fee_from_fills_quote += self._fee_to_quote(fill.get("fee"), fill.get("feeCurrency"), price)

        filled_base = self._to_float(order_state.get("filledAmount"))
        filled_quote = self._to_float(order_state.get("filledAmountQuote"))
        fill_price = order_info["limit_price"]

        if total_base > 0 and total_quote > 0:
            fill_price = total_quote / total_base
        elif filled_base > 0 and filled_quote > 0:
            fill_price = filled_quote / filled_base
        else:
            fill_price = self._to_float(order_state.get("price")) or fill_price

        quote_amount = filled_quote or self._to_float(order_info.get("quote_amount"))
        fee_paid_quote = self._fee_to_quote(order_state.get("feePaid"), order_state.get("feeCurrency"), fill_price)
        if fee_paid_quote == 0 and fee_from_fills_quote > 0:
            fee_paid_quote = fee_from_fills_quote
        fee_rate = (fee_paid_quote / quote_amount) if quote_amount > 0 else 0.0
        effective_fill_count = fill_count
        if effective_fill_count == 0 and status == "filled" and quote_amount > 0:
            effective_fill_count = 1

        return {
            "order_id": order_id,
            "side": order_info["side"],
            "quote_amount": quote_amount,
            "fill_price": fill_price,
            "level_index": order_info.get("level_index"),
            "fill_count": effective_fill_count,
            "fee_paid_quote": fee_paid_quote,
            "fee_rate": fee_rate,
        }

    def _price_key(self, price: float) -> str:
        """Normalize price values to the adapter's outbound precision."""
        return self._format_price(price)

    def _is_open_order_status(self, status: str) -> bool:
        """Return True if Bitvavo status represents an open order."""
        return status in {"new", "awaitingTrigger", "partiallyFilled"}

    def _operator_matches(self, item: dict[str, Any]) -> bool:
        """Filter orders to the current operator when operator_id is configured."""
        if self.operator_id is None:
            return True
        raw_operator = item.get("operatorId")
        try:
            return int(raw_operator) == int(self.operator_id)
        except (TypeError, ValueError):
            return False

    def _index_open_orders_by_side_price(self, items: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
        """Create a side/price lookup map for open orders of this bot/operator."""
        by_side_price: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue

            status = str(item.get("status", "") or "")
            if status and not self._is_open_order_status(status):
                continue

            if not self._operator_matches(item):
                continue

            side = str(item.get("side", "")).lower()
            if side not in {"buy", "sell"}:
                continue

            price = self._to_float(item.get("price"))
            if price <= 0:
                continue

            key = (side, self._price_key(price))
            by_side_price.setdefault(key, []).append(item)
        return by_side_price

    def _index_open_orders_by_client_order_id(self, items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Create a clientOrderId lookup map for open orders of this bot/operator."""
        by_client_order_id: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue

            status = str(item.get("status", "") or "")
            if status and not self._is_open_order_status(status):
                continue

            if not self._operator_matches(item):
                continue

            client_order_id = str(item.get("clientOrderId", "") or "")
            if not client_order_id:
                continue

            by_client_order_id[client_order_id] = item
        return by_client_order_id

    def _build_local_order_id(self, matched: dict[str, Any], level_index: int) -> str:
        """Build a unique local tracking id for an existing exchange order."""
        exchange_oid = str(matched.get("orderId", "") or "")
        local_order_id = str(matched.get("clientOrderId", "") or "")
        if not local_order_id:
            suffix = exchange_oid[:12] if exchange_oid else uuid.uuid4().hex[:12]
            local_order_id = f"existing-{level_index}-{suffix}"
        while local_order_id in self._limit_orders:
            local_order_id = f"{local_order_id}-{uuid.uuid4().hex[:6]}"
        return local_order_id

    def _track_existing_level_order(self, matched: dict[str, Any], level_index: int, planned_side: str, limit_price: float, quote_amount: float) -> None:
        """Register an existing exchange order in local tracking maps."""
        exchange_oid = str(matched.get("orderId", "") or "")
        local_order_id = self._build_local_order_id(matched, level_index)
        client_reference = self._grid_level_reference(level_index)
        client_order_id = str(matched.get("clientOrderId", "") or self._client_order_id(local_order_id, client_reference))
        tracked_side = str(matched.get("side", "") or planned_side).lower()
        tracked_price = self._to_float(matched.get("price")) or limit_price
        self._limit_orders[local_order_id] = {
            "side": tracked_side,
            "quote_amount": float(self._format_amount_quote(quote_amount)),
            "limit_price": float(self._format_price(tracked_price)),
            "level_index": level_index,
            "client_reference": client_reference,
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_oid,
        }
        if exchange_oid:
            self._exchange_order_map[exchange_oid] = local_order_id

    def _take_first_unused(self, candidates: list[dict[str, Any]], used_exchange_ids: set[str]) -> dict[str, Any] | None:
        """Pick the first candidate not already consumed by another level match."""
        for candidate in candidates:
            exchange_oid = str(candidate.get("orderId", "") or "")
            if exchange_oid and exchange_oid in used_exchange_ids:
                continue
            if exchange_oid:
                used_exchange_ids.add(exchange_oid)
            return candidate
        return None

    def get_last_open_order_sync_matches(self) -> list[dict[str, Any]]:
        """Return diagnostics for the most recent open-order sync."""
        return [dict(item) for item in self._last_sync_matches]

    def _mark_exchange_order_processed(self, exchange_order_id: str) -> None:
        """Remember finalized exchange orders to avoid duplicate fill emission."""
        if not exchange_order_id:
            return
        self._processed_exchange_order_ids.add(exchange_order_id)
        if len(self._processed_exchange_order_ids) > 10000:
            self._processed_exchange_order_ids = set(list(self._processed_exchange_order_ids)[-5000:])

    def reconcile_planned_level_orders(
        self,
        planned_open_orders: dict[int, str],
        level_prices: list[float],
        quote_amount: float,
    ) -> list[dict[str, Any]]:
        """Reconcile planned level orders against Bitvavo and return newly finalized fills."""
        if not self.authenticated:
            raise RuntimeError(self._not_authenticated_error)

        now = time.time()
        if now - self._last_planned_level_reconcile_at < self._planned_level_reconcile_interval_seconds:
            return []
        self._last_planned_level_reconcile_at = now

        try:
            open_items = self._get_open_orders()
        except Exception:
            return []

        open_by_client_order_id = self._index_open_orders_by_client_order_id(open_items)
        fills: list[dict[str, Any]] = []

        for level_index in sorted(planned_open_orders.keys()):
            if level_index < 0 or level_index >= len(level_prices):
                continue

            planned_side = str(planned_open_orders[level_index]).lower()
            expected_reference = self._grid_level_reference(level_index)
            expected_client_order_id = self._client_order_id(f"sync-{level_index}", expected_reference)

            if expected_client_order_id in open_by_client_order_id:
                continue

            # If locally tracked, regular order polling handles this order path.
            tracked_locally = any(
                int(info.get("level_index", -1)) == level_index
                for info in self._limit_orders.values()
            )
            if tracked_locally:
                continue

            try:
                response = self._call_action(
                    "privateGetOrder",
                    {"market": self.market, "clientOrderId": expected_client_order_id},
                    timeout=6.0,
                )
            except Exception:
                continue

            if response.get("errorCode") is not None:
                continue

            order_state = self._extract_action_response(response)
            if not isinstance(order_state, dict):
                continue

            status = str(order_state.get("status", "") or "")
            exchange_order_id = str(order_state.get("orderId", "") or "")
            if exchange_order_id and exchange_order_id in self._processed_exchange_order_ids:
                continue

            if status != "filled":
                continue

            order_info = {
                "side": str(order_state.get("side", "") or planned_side).lower(),
                "quote_amount": quote_amount,
                "limit_price": self._to_float(order_state.get("price")) or level_prices[level_index],
                "level_index": level_index,
            }
            logical_id = exchange_order_id or expected_client_order_id
            fill = self._build_fill_from_order_status(f"reconciled-{logical_id}", order_info, order_state)
            fills.append(fill)
            self._mark_exchange_order_processed(exchange_order_id)

        return fills

    def _get_open_orders(self) -> list[dict[str, Any]]:
        """Fetch current open orders, with fallback for legacy/changed action names."""
        open_body = {"market": self.market}
        response = self._call_action("privateGetOrdersOpen", open_body, timeout=10.0)
        if response.get("errorCode") is not None:
            error_text = str(response.get("error") or "")
            if "Invalid action" in error_text:
                # Fallback for API variants where open orders are queried via privateGetOrders.
                list_body = {"market": self.market, "limit": 500}
                response = self._call_action("privateGetOrders", list_body, timeout=10.0)
            else:
                raise self._raise_action_error(
                    "privateGetOrdersOpen",
                    open_body,
                    f"Bitvavo privateGetOrdersOpen failed: {response.get('error') or response.get('errorCode')}",
                )

        if response.get("errorCode") is not None:
            raise self._raise_action_error(
                "privateGetOrders",
                {"market": self.market, "limit": 500},
                f"Bitvavo privateGetOrdersOpen failed: {response.get('error') or response.get('errorCode')}",
            )

        items = response.get("response") if isinstance(response.get("response"), list) else response.get("orders")
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _sync_tracked_order(self, order_id: str) -> dict[str, Any] | None:
        """Refresh one tracked order from Bitvavo and return a fill if finalized."""
        order_info = self._limit_orders.get(order_id)
        if not order_info:
            return None
        exchange_oid = str(order_info.get("exchange_order_id", "") or "")

        request_body: dict[str, Any] = {"market": self.market}
        if exchange_oid:
            request_body["orderId"] = exchange_oid
        else:
            request_body["clientOrderId"] = str(order_info.get("client_order_id") or self._client_order_id(order_id))

        response = self._call_action("privateGetOrder", request_body)
        order_state = self._extract_action_response(response)
        status = str(order_state.get("status", "") or "")

        if status in {"new", "awaitingTrigger", "partiallyFilled"}:
            return None

        if status == "filled":
            fill = self._build_fill_from_order_status(order_id, order_info, order_state)
            self._remove_tracked_order(order_id)
            self._mark_exchange_order_processed(exchange_oid)
            return fill

        if status in {"canceled", "expired"}:
            self._remove_tracked_order(order_id)
            self._mark_exchange_order_processed(exchange_oid)
        return None

    def _reconcile_tracked_orders_with_open_orders(self) -> set[str]:
        """Reconcile tracked orders against Bitvavo open orders before detail polling."""
        if not self._limit_orders:
            return set()

        now = time.time()
        if now - self._last_open_order_refresh_at < self._open_order_refresh_interval_seconds:
            return set()
        self._last_open_order_refresh_at = now

        try:
            open_items = self._get_open_orders()
        except Exception:
            return set()

        open_exchange_ids: set[str] = set()
        open_client_order_ids: set[str] = set()
        for item in open_items:
            if not isinstance(item, dict):
                continue
            if not self._operator_matches(item):
                continue
            status = str(item.get("status", "") or "")
            if status and not self._is_open_order_status(status):
                continue

            exchange_oid = str(item.get("orderId", "") or "")
            if exchange_oid:
                open_exchange_ids.add(exchange_oid)

            client_order_id = str(item.get("clientOrderId", "") or "")
            if client_order_id:
                open_client_order_ids.add(client_order_id)

        missing_order_ids: list[str] = []
        checked_order_ids: set[str] = set()
        for order_id, order_info in self._limit_orders.items():
            exchange_oid = str(order_info.get("exchange_order_id", "") or "")
            client_order_id = str(order_info.get("client_order_id", "") or "")
            still_open = (exchange_oid and exchange_oid in open_exchange_ids) or (
                client_order_id and client_order_id in open_client_order_ids
            )
            if still_open:
                continue
            missing_order_ids.append(order_id)

        for order_id in missing_order_ids:
            checked_order_ids.add(order_id)
            try:
                fill = self._sync_tracked_order(order_id)
            except Exception:
                continue
            if fill is not None:
                with self._fills_lock:
                    self._fills.append(fill)
        return checked_order_ids

    def _next_request_id(self) -> int:
        """Thread-safe auto-incrementing request ID."""
        with self.lock:
            rid = self.request_id
            self.request_id += 1
            return rid

    def _create_signature(self, timestamp_ms: int) -> str:
        """
        Compute the HMAC-SHA256 signature for websocket authentication.

        :param timestamp_ms: Current timestamp in milliseconds.
        :return: Hex-encoded HMAC-SHA256 signature string.
        """
        payload = f"{timestamp_ms}GET/v2/websocket"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _send_json(self, payload: dict[str, Any]) -> None:
        """
        Send a JSON-encoded payload on the websocket.

        :param payload: Dictionary to serialize and send.
        :raises RuntimeError: If the websocket is not connected.
        """
        if not self.ws:
            raise RuntimeError("Bitvavo websocket is not connected")
        self.ws.send(json.dumps(payload))

    def _call_action(
        self,
        action: str,
        body: dict[str, Any],
        timeout: float = 6.0,
        retry_transport: bool = True,
    ) -> dict[str, Any]:
        """
        Send an action and block until the response arrives or timeout elapses.

        :param action: The Bitvavo websocket action name.
        :param body: Request body parameters.
        :param timeout: Maximum seconds to wait for a response.
        :return: The parsed response dictionary.
        :raises TimeoutError: If no response arrives within the timeout.
        """
        attempts = self._action_send_retry_attempts if retry_transport else 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            request_id = self._next_request_id()
            event = threading.Event()
            self.pending_events[request_id] = event

            payload = dict(body)
            payload["action"] = action
            payload["requestId"] = request_id
            try:
                self._send_json(payload)
            except Exception as exc:
                self.pending_events.pop(request_id, None)
                self.pending_responses.pop(request_id, None)
                last_error = exc
                if retry_transport and attempt < attempts - 1 and self._is_transient_transport_error(exc):
                    self._reconnect_transport(str(exc))
                    continue
                raise self._raise_action_error(action, payload, f"Bitvavo websocket send failed: {exc}") from exc

            if not event.wait(timeout=timeout):
                self.pending_events.pop(request_id, None)
                self.pending_responses.pop(request_id, None)
                timeout_exc = TimeoutError(
                    f"Bitvavo websocket action timeout for {action} | {self._request_context(action, payload)}"
                )
                last_error = timeout_exc
                if retry_transport and attempt < attempts - 1:
                    self._reconnect_transport(str(timeout_exc))
                    continue
                raise timeout_exc

            response = self.pending_responses.pop(request_id, {})
            self.pending_events.pop(request_id, None)
            return response

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"Bitvavo action failed unexpectedly for {action}")

    def _extract_price(self, message: dict[str, Any]) -> float | None:
        """
        Try to extract a price from a websocket message.

        Checks 'price', 'last', then midpoint of 'bestBid'/'bestAsk'.

        :param message: Parsed websocket message dictionary.
        :return: Extracted price as float, or None if not found.
        """
        for key in ("price", "last"):
            if key in message:
                try:
                    return float(message[key])
                except (TypeError, ValueError):
                    pass

        best_bid = message.get("bestBid")
        best_ask = message.get("bestAsk")
        if best_bid is not None and best_ask is not None:
            try:
                return (float(best_bid) + float(best_ask)) / 2.0
            except (TypeError, ValueError):
                pass

        return None

    def _fetch_latest_price_from_exchange(self, timeout_seconds: float = 5.0) -> float | None:
        """Fetch current market price directly from Bitvavo REST ticker."""
        try:
            response = requests.get(
                f"{self.rest_url}/ticker/price",
                params={"market": self.market},
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:
            return None

        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and str(item.get("market", "")).upper() == self.market.upper():
                    price = self._to_float(item.get("price"))
                    return price if price > 0 else None
            return None

        if isinstance(payload, dict):
            price = self._to_float(payload.get("price"))
            return price if price > 0 else None

        return None

    def _has_pending_fills(self) -> bool:
        """Return whether websocket/order polling has queued unprocessed fills."""
        with self._fills_lock:
            return bool(self._fills)

    def _handle_message(self, message_text: str) -> None:
        """
        Parse a raw websocket message and dispatch it.

        Routes request/response pairs to pending events and updates
        the latest price from ticker or market messages.

        :param message_text: Raw JSON string received from the websocket.
        """
        try:
            message = json.loads(message_text)
        except json.JSONDecodeError:
            return

        if isinstance(message, dict) and "requestId" in message:
            request_id = int(message["requestId"])
            self.pending_responses[request_id] = message
            pending = self.pending_events.get(request_id)
            if pending:
                pending.set()
            return

        if not isinstance(message, dict):
            return

        if message.get("event") in {"ticker", "trade", "ticker24h"}:
            price = self._extract_price(message)
            if price is not None:
                self.latest_price = price
                self.price_update_event.set()
                self._market_activity_event.set()
            return

        # Order fill events from the account channel
        if message.get("event") == "fill":
            self._handle_fill_event(message)
            return

        if message.get("event") == "order":
            status = message.get("status", "")
            if status in ("filled", "partiallyFilled"):
                self._handle_fill_event(message)
            return

        if message.get("market") == self.market:
            price = self._extract_price(message)
            if price is not None:
                self.latest_price = price
                self.price_update_event.set()
                self._market_activity_event.set()

    def _reader_loop(self) -> None:
        """Background loop that reads from the websocket until stopped."""
        while self.running and self.ws:
            try:
                raw = self.ws.recv()
                if raw:
                    self._handle_message(raw)
            except Exception:
                break
        self.running = False

    def start(self) -> None:
        """Open the websocket, authenticate, and subscribe to ticker events."""
        if self.running:
            return
        self._stop_requested = False
        self._connect_and_authenticate()
        self._load_market_precision()

        # Prime balances once after startup.
        self._refresh_balances()

    def stop(self) -> None:
        """Close the websocket and stop the reader thread."""
        self._stop_requested = True
        self.running = False
        self.authenticated = False
        self._close_socket()

    def _refresh_balances(self) -> None:
        """Fetch latest balances from Bitvavo and update local state."""
        if not self.authenticated:
            return
        response: dict[str, Any] | None = None
        for attempt in range(2):
            try:
                response = self._call_action("privateGetBalance", {}, timeout=10.0)
                break
            except TimeoutError:
                if attempt == 1:
                    logger.warning("Bitvavo privateGetBalance timed out; continuing without refreshed balances")
                    return

        if response is None:
            return

        if response.get("errorCode") is not None:
            raise self._raise_action_error(
                "privateGetBalance",
                {},
                f"Bitvavo privateGetBalance failed: {response.get('error') or response.get('errorCode')}",
            )

        items = response.get("response") if isinstance(response.get("response"), list) else response.get("balances")
        if not isinstance(items, list):
            return

        for item in items:
            symbol = item.get("symbol")
            available = item.get("available")
            try:
                amount = float(available)
            except (TypeError, ValueError):
                continue
            if symbol == self.quote_currency:
                self.quote_balance = amount
            if symbol == self.base_currency:
                self.base_balance = amount

    def get_price(self, fallback_price: float | None = None) -> float:
        """
        Return the latest known price, or fallback_price if not yet received.

        :param fallback_price: Price to return when no live price is available.
        :return: The current market price.
        :raises RuntimeError: If no price is available and no fallback is given.
        """
        if self.latest_price is not None:
            return self.latest_price
        if fallback_price is not None:
            return fallback_price
        raise RuntimeError("Bitvavo price is not available yet")

    def wait_for_price_update(self, last_price: float | None = None, timeout_seconds: float = 15.0) -> float:
        """
        Block until a price different from last_price arrives.

        :param last_price: The previous price to compare against.
        :param timeout_seconds: Maximum seconds to wait for a new price.
        :return: The updated market price.
        """
        current = self.latest_price
        if current is not None and current != last_price:
            return current

        # Allow runner loops to react to websocket fills immediately,
        # even when price has not moved.
        if self._has_pending_fills():
            if current is not None:
                return current
            if last_price is not None:
                return last_price

        self.price_update_event.clear()
        self._market_activity_event.clear()
        has_update = self._market_activity_event.wait(timeout=timeout_seconds)
        if has_update and self.latest_price is not None:
            return self.latest_price
        if has_update and last_price is not None:
            return last_price

        # If no WS tick arrived yet (common at startup), fetch real market price
        # directly from Bitvavo to avoid failing live bot startup.
        fetched = self._fetch_latest_price_from_exchange(timeout_seconds=min(5.0, timeout_seconds))
        if fetched is not None:
            self.latest_price = fetched
            return fetched

        return self.get_price(last_price)

    def get_balances(self) -> tuple[float, float]:
        """Refresh and return ``(quote_balance, base_balance)`` from Bitvavo."""
        self._refresh_balances()
        return self.quote_balance, self.base_balance

    def execute(self, signal: TradeSignal, price: float | None = None) -> bool:
        """
        Place a market order for the given signal.

        :param signal: The trade signal describing side and quote amount.
        :param price: The market price for calculating sell amounts.
        :return: True if the order succeeded, False on error.
        :raises RuntimeError: If the websocket is not authenticated.
        """
        if not self.authenticated:
            raise RuntimeError(self._not_authenticated_error)

        market_price = price if price is not None else self.get_price(None)
        body: dict[str, Any] = {
            "market": self.market,
            "orderType": "market",
            "side": signal.side,
            "operatorId": self.operator_id,
        }

        if signal.side == "buy":
            body["amountQuote"] = self._format_amount_quote(signal.quote_amount)
        else:
            if market_price is None or market_price <= 0:
                raise RuntimeError("Cannot calculate sell amount without valid market price")
            amount_base = signal.quote_amount / market_price
            body["amount"] = self._format_amount_base(amount_base)

        response = self._call_action("privateCreateOrder", body)
        if response.get("errorCode") is not None:
            return False

        self._refresh_balances()
        return True

    # ── Limit orders ──────────────────────────────────────────

    def _handle_fill_event(self, message: dict[str, Any]) -> None:
        """React to account-channel order progress by syncing the full order state."""
        exchange_oid = message.get("orderId", "")
        our_oid = self._exchange_order_map.get(exchange_oid)
        if not our_oid or our_oid not in self._limit_orders:
            return

        try:
            fill = self._sync_tracked_order(our_oid)
        except Exception:
            return

        if fill is not None:
            with self._fills_lock:
                self._fills.append(fill)
            self._market_activity_event.set()

    def place_limit_order(
        self,
        order_id: str,
        side: str,
        quote_amount: float,
        limit_price: float,
        level_index: int | None = None,
        client_reference: str | None = None,
    ) -> bool:
        """Place a limit order on Bitvavo at the given price.

        :return: True if the order was accepted by the exchange.
        """
        if not self.authenticated:
            raise RuntimeError(self._not_authenticated_error)

        body: dict[str, Any] = {
            "market": self.market,
            "orderType": "limit",
            "side": side,
            "operatorId": self.operator_id,
            "price": self._format_price(limit_price),
            "postOnly": True,
        }
        stable_reference = client_reference or self._grid_level_reference(level_index)
        client_order_id = self._client_order_id(order_id, stable_reference)
        body["clientOrderId"] = client_order_id

        amount_base = quote_amount / limit_price
        body["amount"] = self._format_amount_base(amount_base)

        try:
            response = self._call_action("privateCreateOrder", body, timeout=10.0)
        except TimeoutError:
            # The order may still be accepted by the exchange; check by clientOrderId.
            try:
                response = self._call_action(
                    "privateGetOrder",
                    {"market": self.market, "clientOrderId": client_order_id},
                    timeout=6.0,
                )
            except Exception as exc:
                raise RuntimeError(f"Bitvavo privateCreateOrder failed: {exc}") from exc

        if response.get("errorCode") is not None:
            raise self._raise_action_error(
                "privateCreateOrder",
                body,
                f"Bitvavo privateCreateOrder failed: {response.get('error') or response.get('errorCode')}",
            )

        resp = self._extract_action_response(response)
        exchange_oid = str(resp.get("orderId", "") or response.get("orderId", ""))

        self._limit_orders[order_id] = {
            "side": side,
            "quote_amount": float(self._format_amount_quote(quote_amount)),
            "limit_price": float(self._format_price(limit_price)),
            "level_index": level_index,
            "client_reference": stable_reference,
            "client_order_id": client_order_id,
            "exchange_order_id": exchange_oid,
        }
        if exchange_oid:
            self._exchange_order_map[exchange_oid] = order_id
        return True

    def get_filled_orders(self) -> list[dict[str, Any]]:
        """Return finalized fills and keep polling active Bitvavo orders."""
        reconciled_order_ids = self._reconcile_tracked_orders_with_open_orders()

        for order_id in tuple(self._limit_orders):
            if order_id in reconciled_order_ids:
                continue
            try:
                fill = self._sync_tracked_order(order_id)
            except Exception:
                continue
            if fill is not None:
                with self._fills_lock:
                    self._fills.append(fill)

        with self._fills_lock:
            fills = list(self._fills)
            self._fills.clear()
        self._refresh_balances()
        return fills

    def has_tracked_level_order(self, level_index: int, side: str, limit_price: float) -> bool:
        """Return whether a matching open order is already tracked locally."""
        expected_side = str(side).lower()
        expected_price = self._price_key(limit_price)
        for info in self._limit_orders.values():
            info_level = info.get("level_index")
            if info_level == level_index:
                return True
            info_side = str(info.get("side", "")).lower()
            info_price = self._price_key(self._to_float(info.get("limit_price")))
            if info_level == level_index and info_side == expected_side and info_price == expected_price:
                return True
        return False

    def sync_open_orders_for_levels(
        self,
        planned_open_orders: dict[int, str],
        level_prices: list[float],
        quote_amount: float,
    ) -> set[int]:
        """Track already-open Bitvavo orders for this bot/operator and planned levels."""
        if not self.authenticated:
            raise RuntimeError(self._not_authenticated_error)

        self._last_sync_matches = []
        items = self._get_open_orders()
        if not items:
            return set()

        by_client_order_id = self._index_open_orders_by_client_order_id(items)
        by_side_price = self._index_open_orders_by_side_price(items)

        matched_levels: set[int] = set()
        used_exchange_ids: set[str] = set()

        for level_index in sorted(planned_open_orders.keys()):
            if level_index < 0 or level_index >= len(level_prices):
                continue

            planned_side = str(planned_open_orders[level_index]).lower()
            expected_reference = self._grid_level_reference(level_index)
            expected_client_order_id = self._client_order_id(f"sync-{level_index}", expected_reference)
            matched = by_client_order_id.get(expected_client_order_id)
            match_method = "client_reference"
            if matched is not None:
                exchange_oid = str(matched.get("orderId", "") or "")
                if exchange_oid:
                    used_exchange_ids.add(exchange_oid)
            else:
                match_method = "price_fallback"
                planned_price = self._price_key(level_prices[level_index])
                key = (planned_side, planned_price)
                candidates = by_side_price.get(key, [])
                matched = self._take_first_unused(candidates, used_exchange_ids)

            if matched is None:
                continue

            self._track_existing_level_order(
                matched=matched,
                level_index=level_index,
                planned_side=planned_side,
                limit_price=level_prices[level_index],
                quote_amount=quote_amount,
            )
            self._last_sync_matches.append(
                {
                    "level_index": level_index,
                    "match_method": match_method,
                    "client_reference": expected_reference,
                    "client_order_id": str(matched.get("clientOrderId", "") or expected_client_order_id),
                    "exchange_order_id": str(matched.get("orderId", "") or ""),
                    "side": str(matched.get("side", "") or planned_side).lower(),
                    "price": self._to_float(matched.get("price")) or level_prices[level_index],
                }
            )
            matched_levels.add(level_index)

        return matched_levels

    def cancel_all_orders(self) -> None:
        """Cancel all pending limit orders on Bitvavo."""
        for order_id, info in self._limit_orders.items():
            exchange_oid = info.get("exchange_order_id", "")
            if exchange_oid:
                try:
                    self._call_action("privateCancelOrder", {
                        "market": self.market,
                        "orderId": exchange_oid,
                    })
                except Exception:
                    pass
                self._exchange_order_map.pop(exchange_oid, None)
        self._limit_orders.clear()
