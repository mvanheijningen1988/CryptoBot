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
    def __init__(
        self,
        api_key: str,
        api_secret: str,
        market: str,
        base_currency: str,
        quote_currency: str,
        ws_url: str = "wss://ws.bitvavo.com/v2/",
    ):
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

    def _next_request_id(self) -> int:
        with self.lock:
            rid = self.request_id
            self.request_id += 1
            return rid

    def _create_signature(self, timestamp_ms: int) -> str:
        payload = f"{timestamp_ms}GET/v2/websocket"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            payload.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _send_json(self, payload: dict[str, Any]) -> None:
        if not self.ws:
            raise RuntimeError("Bitvavo websocket is not connected")
        self.ws.send(json.dumps(payload))

    def _call_action(self, action: str, body: dict[str, Any], timeout: float = 6.0) -> dict[str, Any]:
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

        if message.get("market") == self.market:
            price = self._extract_price(message)
            if price is not None:
                self.latest_price = price
                self.price_update_event.set()

    def _reader_loop(self) -> None:
        while self.running and self.ws:
            try:
                raw = self.ws.recv()
                if raw:
                    self._handle_message(raw)
            except Exception:
                break
        self.running = False

    def start(self) -> None:
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
                    {
                        "name": "ticker",
                        "markets": [self.market],
                    }
                ],
            }
        )

        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        self.authenticated = True

        # Prime balances once after startup.
        self._refresh_balances()

    def stop(self) -> None:
        self.running = False
        self.authenticated = False
        if self.ws:
            try:
                self.ws.close()
            except Exception:
                pass
        self.ws = None

    def _refresh_balances(self) -> None:
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
        if self.latest_price is not None:
            return self.latest_price
        if fallback_price is not None:
            return fallback_price
        raise RuntimeError("Bitvavo price is not available yet")

    def wait_for_price_update(self, last_price: float | None = None, timeout_seconds: float = 15.0) -> float:
        current = self.latest_price
        if current is not None and current != last_price:
            return current

        self.price_update_event.clear()
        has_update = self.price_update_event.wait(timeout=timeout_seconds)
        if has_update and self.latest_price is not None:
            return self.latest_price

        return self.get_price(last_price)

    def get_balances(self) -> tuple[float, float]:
        self._refresh_balances()
        return self.quote_balance, self.base_balance

    def execute(self, signal: TradeSignal, price: float | None = None) -> bool:
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
