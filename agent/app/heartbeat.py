"""Agent registration and periodic heartbeat with the manager."""
from __future__ import annotations

import time

import requests

from agent.app.config import AGENT_BASE_URL, AGENT_ID, MANAGER_URL, runner_manager
from agent.app.version import __version__


def register_agent() -> bool:
    """
    Register this agent with the manager service.

    :return: True if registration succeeded, False otherwise.
    """
    payload = {
        "agent_id": AGENT_ID,
        "base_url": AGENT_BASE_URL,
        "capacity": 10,
        "version": __version__,
    }
    try:
        response = requests.post(f"{MANAGER_URL}/api/v1/agents/register", json=payload, timeout=5)
        if response.status_code < 400:
            runner_manager.log_system("register_ok", "Agent registered to manager.", {"manager_url": MANAGER_URL})
        else:
            runner_manager.log_system(
                "register_failed",
                "Agent registration returned an error.",
                {"status_code": response.status_code, "body": response.text[:300]},
            )
        return response.status_code < 400
    except requests.RequestException:
        runner_manager.log_system("register_failed", "Agent registration request failed.")
        return False


def heartbeat_loop() -> None:
    """
    Periodically ping the manager to confirm this agent is alive.

    Runs indefinitely in a daemon thread, re-registering if the
    manager loses track of the agent.
    """
    registered = False
    last_state: str | None = None
    while True:
        if not registered:
            registered = register_agent()

        try:
            response = requests.post(
                f"{MANAGER_URL}/api/v1/agents/{AGENT_ID}/heartbeat",
                json={"status": "online", "version": __version__},
                timeout=5,
            )
            if response.status_code == 404:
                registered = False
            state = "online" if response.status_code < 400 else f"heartbeat_error_{response.status_code}"
            if state != last_state:
                msg = f"Agent heartbeat state changed from '{last_state or 'unknown'}' to '{state}'."
                runner_manager.log_system(
                    "heartbeat_state",
                    msg,
                    {"previous_state": last_state, "state": state},
                )
                last_state = state
        except requests.RequestException:
            registered = False
            if last_state != "heartbeat_unreachable":
                runner_manager.log_system("heartbeat_state", "Manager heartbeat endpoint unreachable.")
                last_state = "heartbeat_unreachable"
        time.sleep(10)
