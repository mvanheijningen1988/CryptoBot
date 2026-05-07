"""Live Bitvavo exchange adapter using websocket API v2.

Authenticates via HMAC-SHA256, subscribes to ticker events for real-time
prices, and executes market orders through the websocket action API.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time
from typing import Any

import websocket

from common.exchange.base import Exchange
from common.models import TradeSignal


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
        self.market = market
        self.base_currency = base_currency
        self.quote_currency = quote_currency
        self.ws_url = ws_url

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

        # Limit order tracking
        self._limit_orders: dict[str, dict[str, Any]] = {}  # order_id → order info
        self._exchange_order_map: dict[str, str] = {}  # exchange_order_id → our order_id
        self._fills: list[dict[str, Any]] = []
        self._fills_lock = threading.Lock()

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

    def _call_action(self, action: str, body: dict[str, Any], timeout: float = 6.0) -> dict[str, Any]:
        """
        Send an action and block until the response arrives or timeout elapses.

        :param action: The Bitvavo websocket action name.
        :param body: Request body parameters.
        :param timeout: Maximum seconds to wait for a response.
        :return: The parsed response dictionary.
        :raises TimeoutError: If no response arrives within the timeout.
        """
        request_id = self._next_request_id()
        event = threading.Event()
        self.pending_events[request_id] = event

        payload = dict(body)
        payload["action"] = action
        payload["requestId"] = request_id
        self._send_json(payload)

        if not event.wait(timeout=timeout):
            self.pending_events.pop(request_id, None)
            self.pending_responses.pop(request_id, None)
            raise TimeoutError(f"Bitvavo websocket action timeout for {action}")

        response = self.pending_responses.pop(request_id, {})
        self.pending_events.pop(request_id, None)
        return response

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

        self.ws = websocket.create_connection(self.ws_url, timeout=10)
        self.running = True

        timestamp_ms = int(time.time() * 1000)
        signature = self._create_signature(timestamp_ms)
        self._send_json(
            {
                "action": "authenticate",
                "key": self.api_key,
                "signature": signature,
                "timestamp": timestamp_ms,
            }
        )

        # Subscribe to ticker updates for the configured market.
        self._send_json(
            {
                "action": "subscribe",
                "channels": [
                    {"name": "ticker", "markets": [self.market]},
                    {"name": "account", "markets": [self.market]},
                ],
            }
        )

        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        self.authenticated = True

        # Prime balances once after startup.
        self._refresh_balances()

    def stop(self) -> None:
        """Close the websocket and stop the reader thread."""
        self.running = False
        self.authenticated = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None

    def _refresh_balances(self) -> None:
        """Fetch latest balances from Bitvavo and update local state."""
        if not self.authenticated:
            return
        response = self._call_action("privateGetBalance", {})
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

        self.price_update_event.clear()
        has_update = self.price_update_event.wait(timeout=timeout_seconds)
        if has_update and self.latest_price is not None:
            return self.latest_price

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
            raise RuntimeError("Bitvavo websocket is not authenticated")

        market_price = price if price is not None else self.get_price(None)
        body: dict[str, Any] = {
            "market": self.market,
            "orderType": "market",
            "side": signal.side,
        }

        if signal.side == "buy":
            body["amountQuote"] = f"{signal.quote_amount:.8f}"
        else:
            if market_price is None or market_price <= 0:
                raise RuntimeError("Cannot calculate sell amount without valid market price")
            amount_base = signal.quote_amount / market_price
            body["amount"] = f"{amount_base:.8f}"

        response = self._call_action("privateCreateOrder", body)
        if response.get("errorCode") is not None:
            return False

        self._refresh_balances()
        return True

    # ── Limit orders ──────────────────────────────────────────

    def _handle_fill_event(self, message: dict[str, Any]) -> None:
        """Process an order fill event from the WS account channel."""
        exchange_oid = message.get("orderId", "")
        our_oid = self._exchange_order_map.get(exchange_oid)
        if not our_oid or our_oid not in self._limit_orders:
            return

        order_info = self._limit_orders[our_oid]
        fill_price = order_info["limit_price"]
        try:
            fill_price = float(message.get("price", fill_price))
        except (TypeError, ValueError):
            pass

        with self._fills_lock:
            self._fills.append({
                "order_id": our_oid,
                "side": order_info["side"],
                "quote_amount": order_info["quote_amount"],
                "fill_price": fill_price,
                "level_index": order_info.get("level_index"),
            })

        del self._limit_orders[our_oid]
        del self._exchange_order_map[exchange_oid]

    def place_limit_order(
        self,
        order_id: str,
        side: str,
        quote_amount: float,
        limit_price: float,
        level_index: int | None = None,
    ) -> bool:
        """Place a limit order on Bitvavo at the given price.

        :return: True if the order was accepted by the exchange.
        """
        if not self.authenticated:
            raise RuntimeError("Bitvavo websocket is not authenticated")

        body: dict[str, Any] = {
            "market": self.market,
            "orderType": "limit",
            "side": side,
            "price": f"{limit_price:.8f}",
        }

        if side == "buy":
            amount_base = quote_amount / limit_price
            body["amount"] = f"{amount_base:.8f}"
        else:
            amount_base = quote_amount / limit_price
            body["amount"] = f"{amount_base:.8f}"

        response = self._call_action("privateCreateOrder", body)
        if response.get("errorCode") is not None:
            return False

        exchange_oid = response.get("orderId", "")
        if not exchange_oid:
            resp = response.get("response", {})
            if isinstance(resp, dict):
                exchange_oid = resp.get("orderId", "")

        self._limit_orders[order_id] = {
            "side": side,
            "quote_amount": quote_amount,
            "limit_price": limit_price,
            "level_index": level_index,
            "exchange_order_id": exchange_oid,
        }
        if exchange_oid:
            self._exchange_order_map[exchange_oid] = order_id
        return True

    def get_filled_orders(self) -> list[dict[str, Any]]:
        """Return and clear accumulated fill events."""
        with self._fills_lock:
            fills = list(self._fills)
            self._fills.clear()
        self._refresh_balances()
        return fills

    def cancel_all_orders(self) -> None:
        """Cancel all pending limit orders on Bitvavo."""
        for order_id, info in list(self._limit_orders.items()):
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
