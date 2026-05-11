from __future__ import annotations

import asyncio
import threading
import time

import pytest

from manager.app.services import agent_ws_bus


class _FakeFuture:
	def result(self, timeout=None):
		return None


class _FakeWebSocket:
	def __init__(self) -> None:
		self.sent_payloads: list[str] = []

	async def send_text(self, payload: str) -> None:
		await asyncio.sleep(0)
		self.sent_payloads.append(payload)


def _run_coroutine_threadsafe_stub(coro, _loop):
	coro.close()
	return _FakeFuture()


@pytest.fixture(autouse=True)
def _clear_bus_sessions():
	with agent_ws_bus._SESSIONS_LOCK:
		agent_ws_bus._SESSIONS.clear()
	yield
	with agent_ws_bus._SESSIONS_LOCK:
		agent_ws_bus._SESSIONS.clear()


def test_send_agent_command_ws_returns_no_session_when_missing():
	ok, message, data = agent_ws_bus.send_agent_command_ws(
		agent_id="missing-agent",
		action="start_bot",
		payload={"bot_id": "bot-1"},
	)

	assert ok is False
	assert message == "no_ws_session"
	assert data is None


def test_send_agent_command_ws_resolves_response(monkeypatch):
	websocket = _FakeWebSocket()
	session = agent_ws_bus.register_agent_ws_session(
		agent_id="agent-1",
		websocket=websocket,
		loop=object(),
	)

	monkeypatch.setattr(
		agent_ws_bus.asyncio,
		"run_coroutine_threadsafe",
		_run_coroutine_threadsafe_stub,
	)

	result_holder: dict = {}

	def _worker() -> None:
		result_holder["result"] = agent_ws_bus.send_agent_command_ws(
			agent_id="agent-1",
			action="sync_bot",
			payload={"bot_id": "bot-1"},
			timeout_seconds=1.0,
		)

	thread = threading.Thread(target=_worker)
	thread.start()

	deadline = time.time() + 1.0
	message_id = ""
	while time.time() < deadline and not message_id:
		with session.lock:
			pending_ids = list(session.pending.keys())
		if pending_ids:
			message_id = pending_ids[0]
			break
		time.sleep(0.01)

	assert message_id
	agent_ws_bus.resolve_agent_command_response(
		agent_id="agent-1",
		message_id=message_id,
		payload={"ok": True, "message": "synced", "data": {"details": {"x": 1}}},
	)

	thread.join(timeout=2.0)
	assert "result" in result_holder
	assert result_holder["result"] == (True, "synced", {"details": {"x": 1}})


def test_send_agent_command_ws_returns_timeout(monkeypatch):
	websocket = _FakeWebSocket()
	agent_ws_bus.register_agent_ws_session(
		agent_id="agent-timeout",
		websocket=websocket,
		loop=object(),
	)

	monkeypatch.setattr(
		agent_ws_bus.asyncio,
		"run_coroutine_threadsafe",
		_run_coroutine_threadsafe_stub,
	)

	ok, message, data = agent_ws_bus.send_agent_command_ws(
		agent_id="agent-timeout",
		action="stop_bot",
		payload={"bot_id": "bot-1"},
		timeout_seconds=0.05,
	)

	assert ok is False
	assert message == "ws_timeout"
	assert data is None


def test_send_agent_command_ws_returns_send_failed(monkeypatch):
	websocket = _FakeWebSocket()
	agent_ws_bus.register_agent_ws_session(
		agent_id="agent-send-fail",
		websocket=websocket,
		loop=object(),
	)

	def _raise(*_args, **_kwargs):
		raise RuntimeError("send-broken")

	monkeypatch.setattr(agent_ws_bus.asyncio, "run_coroutine_threadsafe", _raise)

	ok, message, data = agent_ws_bus.send_agent_command_ws(
		agent_id="agent-send-fail",
		action="start_bot",
		payload={"bot_id": "bot-1"},
	)

	assert ok is False
	assert "ws_send_failed" in message
	assert "send-broken" in message
	assert data is None
