"""CryptoBot Manager – FastAPI application entry point.

Wires together middleware, static files, route modules, and background
tasks.  All endpoint logic lives in ``manager.app.routes.*``.
"""
from __future__ import annotations

import os
from pathlib import Path
from threading import Thread

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from manager.app.auth import ensure_admin_user
from manager.app.database import Base, SessionLocal, engine
from manager.app.failover import failover_maintenance_loop
from manager.app.migrations import run_migrations
from manager.app.routes import v1
from manager.app.services.coin_map_sync import coin_map_sync_loop
from manager.app.version import __version__

# ── Database bootstrap ──────────────────────────────────────────────
Base.metadata.create_all(bind=engine)
run_migrations(engine)

# ── Application ─────────────────────────────────────────────────────
app = FastAPI(title="CryptoBot Manager", version=__version__)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
app.include_router(v1)


# ── Startup ─────────────────────────────────────────────────────────
@app.on_event("startup")
def startup_event() -> None:
    """Bootstrap the admin user and start the failover maintenance thread."""
    db = SessionLocal()
    try:
        ensure_admin_user(db)
    finally:
        db.close()
    thread = Thread(target=failover_maintenance_loop, args=(SessionLocal,), daemon=True)
    thread.start()
    coin_map_thread = Thread(target=coin_map_sync_loop, daemon=True)
    coin_map_thread.start()


# ── Top-level pages & health ────────────────────────────────────────
@app.get("/")
def root() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/login")
def login_page() -> FileResponse:
    """
    Serve the login single-page application.

    :return: FileResponse with the login HTML page.
    """
    return FileResponse(static_dir / "login.html")


@app.get("/health")
def health() -> dict:
    """
    Health check endpoint used by orchestrators and monitoring.

    :return: Dict with status, service name, version, and env.
    """
    return {"status": "ok", "service": "manager", "version": __version__, "env": os.getenv("ENV", "dev")}
