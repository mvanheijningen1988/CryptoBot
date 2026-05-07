"""FastAPI application entry point for the CryptoBot agent service.

Creates the app, registers route handlers, and launches the
heartbeat background thread on startup.
"""
from __future__ import annotations

import threading

from fastapi import FastAPI

from agent.app.config import AGENT_ID, runner_manager
from agent.app.heartbeat import heartbeat_loop, register_agent
from agent.app.routes import router
from agent.app.version import __version__

app = FastAPI(title="CryptoBot Agent", version=__version__)
app.include_router(router)


@app.on_event("startup")
def startup_event() -> None:
    """Register agent and start the heartbeat loop on application start."""
    runner_manager.log_system("agent_startup", "Agent process started.", {"agent_id": AGENT_ID})
    register_agent()
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
