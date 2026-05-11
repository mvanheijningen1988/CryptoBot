"""Persistent websocket client from agent to manager for bidirectional events."""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from urllib.parse import urlencode, urlparse, urlunparse

import websocket

from agent.app.config import AGENT_ID, MANAGER_URL, runner_manager
from agent.app.runtime_settings import apply_runtime_settings, refresh_runtime_settings
from common import BotConfig, BudgetConfig, RunnerState


def _manager_ws_url() -> str:
    parsed = urlparse(MANAGER_URL)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    path = "/api/v1/agents/ws"
    query = urlencode({"agent_id": AGENT_ID})
    return urlunparse((scheme, parsed.netloc, path, "", query, ""))


class ManagerWsClient:
    """Threaded, reconnecting websocket client used by agent internals."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=400)
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._send_lock = threading.Lock()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def enqueue(self, msg_type: str, payload: dict) -> bool:
        message = {
            "type": str(msg_type or "event"),
            "message_id": str(uuid.uuid4()),
            "payload": payload if isinstance(payload, dict) else {},
        }
        try:
            self._queue.put_nowait(message)
            return True
        except queue.Full:
            runner_manager.log_system("ws_queue_full", "Manager websocket queue is full; dropping event.")
            return False

    def _headers(self) -> list[str]:
        token = os.getenv("AGENT_WS_TOKEN", "").strip()
        if not token:
            return []
        return [f"x-agent-ws-token: {token}"]

    def _run(self) -> None:
        ws_url = _manager_ws_url()
        while not self._stop_event.is_set():
            ws = None
            try:
                ws = websocket.create_connection(ws_url, timeout=8, header=self._headers())
                ws.settimeout(1.0)
                self._connected = True
                runner_manager.log_system("ws_connected", "Manager websocket connected.", {"url": ws_url})
                self.enqueue(
                    "agent_event",
                    {
                        "event_type": "ws_connected",
                        "message": f"Agent {AGENT_ID} connected over websocket.",
                    },
                )

                while not self._stop_event.is_set():
                    self._drain_send(ws)
                    self._drain_receive(ws)
            except Exception as exc:
                if self._connected:
                    runner_manager.log_system("ws_disconnected", "Manager websocket disconnected.", {"error": str(exc)})
                self._connected = False
                time.sleep(2.0)
            finally:
                self._connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass

    def _drain_send(self, ws: websocket.WebSocket) -> None:
        sent = 0
        while sent < 40 and not self._stop_event.is_set():
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return
            payload = json.dumps(item, separators=(",", ":"))
            with self._send_lock:
                ws.send(payload)
            sent += 1

    def _drain_receive(self, ws: websocket.WebSocket) -> None:
        try:
            raw = ws.recv()
        except websocket.WebSocketTimeoutException:
            return

        if not raw:
            return

        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return

        msg_type = str(msg.get("type", "") or "").lower()
        if msg_type == "hello":
            runner_manager.log_system("ws_hello", "Manager websocket handshake completed.")
            return
        if msg_type == "ping":
            self.enqueue("pong", {"ts": int(time.time())})
            return
        if msg_type == "error":
            runner_manager.log_system("ws_error", "Manager websocket returned an error.", {"payload": msg})
            return
        if msg_type == "command":
            self._handle_manager_command(ws, msg)

    def _handle_manager_command(self, ws: websocket.WebSocket, message: dict) -> None:
        message_id = str(message.get("message_id", "") or "")
        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        action = str(payload.get("action", "") or "").strip()

        result = self._execute_command(action=action, payload=payload)
        wire = {
            "type": "command_result",
            "message_id": message_id,
            "payload": result,
        }
        with self._send_lock:
            ws.send(json.dumps(wire, separators=(",", ":")))

    def _execute_command(self, action: str, payload: dict) -> dict:
        bot_id = str(payload.get("bot_id", "") or "")
        try:
            if action == "start_bot":
                config = BotConfig(**(payload.get("config") or {}))
                runner_state_raw = payload.get("runner_state")
                runner_state = RunnerState(**runner_state_raw) if isinstance(runner_state_raw, dict) else None
                runner_manager.start_bot(bot_id, config=config, runner_state=runner_state)
                return {"ok": True, "message": "started"}

            if action == "stop_bot":
                runner_manager.stop_bot(bot_id)
                return {"ok": True, "message": "stopped"}

            if action == "sync_bot":
                details = runner_manager.sync_bot(bot_id)
                return {"ok": True, "message": "synced", "data": {"details": details}}

            if action == "prepare_delete":
                delete_mode = str(payload.get("delete_mode", "delete_open_orders") or "delete_open_orders")
                details = runner_manager.prepare_delete(bot_id, delete_mode)
                return {"ok": True, "message": "delete_prepared", "data": {"details": details}}

            if action == "update_budget":
                budget = BudgetConfig(**(payload.get("budget") or {}))
                runner_manager.update_budget(bot_id, budget)
                return {"ok": True, "message": "budget_updated"}

            if action == "refresh_settings":
                try:
                    snapshot = payload.get("settings") if isinstance(payload.get("settings"), dict) else None
                    if not isinstance(snapshot, dict):
                        snapshot = refresh_runtime_settings()
                except Exception as exc:
                    runner_manager.log_system(
                        "settings_refresh_failed",
                        "Failed to refresh runtime settings from manager.",
                        {"error": str(exc)},
                    )
                    return {"ok": False, "message": str(exc)}
                apply_runtime_settings(snapshot)
                runner_manager.reload_runtime_settings(snapshot)
                return {"ok": True, "message": "settings_refreshed"}

            return {"ok": False, "message": f"unknown_action: {action}"}
        except Exception as exc:
            runner_manager.log_system(
                "ws_command_failed",
                "Manager websocket command failed.",
                {"action": action, "bot_id": bot_id, "error": str(exc)},
            )
            return {"ok": False, "message": str(exc)}


manager_ws_client = ManagerWsClient()


def start_manager_ws_client() -> None:
    manager_ws_client.start()


def ws_send_event(msg_type: str, payload: dict) -> bool:
    if not manager_ws_client.connected:
        return False
    return manager_ws_client.enqueue(msg_type, payload)
