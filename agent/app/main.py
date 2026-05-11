"""FastAPI application entry point for the CryptoBot agent service.

Creates the app, registers route handlers, and launches the
heartbeat background thread on startup.
"""
from __future__ import annotations

import threading
from uuid import uuid4
import logging

from fastapi import FastAPI
from fastapi import Request

from common.diagnostics import configure_diagnostics_logging, debug_log, restore_context, set_context
from agent.app.config import AGENT_ID, runner_manager
from agent.app.heartbeat import heartbeat_loop, register_agent
from agent.app.manager_ws import start_manager_ws_client
from agent.app.routes import router
from agent.app.version import __version__

configure_diagnostics_logging("agent")
logger = logging.getLogger(__name__)

app = FastAPI(title="CryptoBot Agent", version=__version__)
app.include_router(router)


@app.middleware("http")
async def diagnostics_context_middleware(request: Request, call_next):
    """Attach correlation/request IDs to agent request handling."""
    correlation_id = request.headers.get("x-correlation-id") or str(uuid4())
    request_id = str(uuid4())
    previous = set_context(
        correlation_id=correlation_id,
        request_id=request_id,
        component="agent.http",
        agent_id=AGENT_ID,
    )
    debug_log(
        logger,
        "http_request_start",
        f"Agent request started: {request.method} {request.url.path}?{request.url.query or ''}",
        method=request.method,
        path=request.url.path,
        query=str(request.url.query or ""),
        agent_id=AGENT_ID,
    )
    try:
        response = await call_next(request)
        response.headers["x-correlation-id"] = correlation_id
        debug_log(
            logger,
            "http_request_end",
            f"Agent request completed: {request.method} {request.url.path} -> {response.status_code}",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            agent_id=AGENT_ID,
        )
        return response
    finally:
        restore_context(previous)


@app.on_event("startup")
def startup_event() -> None:
    """Register agent and start the heartbeat loop on application start."""
    runner_manager.log_system("agent_startup", "Agent process started.", {"agent_id": AGENT_ID})
    register_agent()
    start_manager_ws_client()
    thread = threading.Thread(target=heartbeat_loop, daemon=True)
    thread.start()
