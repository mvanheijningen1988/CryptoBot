"""CryptoBot Manager – FastAPI application.

Exposes REST endpoints for bot CRUD, agent discovery and approval,
authentication, user management, market data proxying, backtesting,
and grid profitability previews.  Also runs a background thread for
agent failover monitoring.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time as _time
import uuid
from datetime import datetime
from pathlib import Path
from threading import Lock
from threading import Thread
from time import sleep

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import requests
from sqlalchemy import func
from sqlalchemy.orm import Session

from manager.app.database import Base, SessionLocal, engine, get_db
from manager.app.models import Agent, Bot, User
from manager.app.auth import (
    create_token,
    decode_token,
    ensure_admin_user,
    get_current_user,
    hash_password,
    require_role,
    verify_password,
)
from manager.app.schemas import (
    AgentHeartbeatRequest,
    AgentRegisterRequest,
    BacktestRequest,
    BacktestResponse,
    BotCreateRequest,
    BotResponse,
    MetricsPushRequest,
    StaticGridPreviewRequest,
    StaticGridPreviewResponse,
    StartBotRequest,
    UpdateBudgetRequest,
)
from manager.app.services.agent_client import post_json
from manager.app.services.backtest import run_backtest
from manager.app.services.grid_preview import build_static_grid_profit_preview

Base.metadata.create_all(bind=engine)


def _ensure_agent_approval_column() -> None:
    """
    Lightweight SQLite migration: add approval_status column to agents if missing.
    """
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql("PRAGMA table_info(agents)").fetchall()
            columns = {row[1] for row in rows}
            if "approval_status" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE agents ADD COLUMN approval_status VARCHAR(32) DEFAULT 'pending'"
                )
                conn.exec_driver_sql(
                    "UPDATE agents SET approval_status='approved' WHERE approval_status IS NULL"
                )
                conn.commit()
        except Exception:
            # Best-effort migration: on unsupported dialects or failures, app keeps running.
            pass


_ensure_agent_approval_column()


def _ensure_users_table() -> None:
    """
    Create the users table if it does not yet exist (SQLite only).
    """
    with engine.connect() as conn:
        try:
            rows = conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchall()
            if not rows:
                conn.exec_driver_sql(
                    """CREATE TABLE users (
                        id VARCHAR(64) PRIMARY KEY,
                        username VARCHAR(128) UNIQUE NOT NULL,
                        password_hash VARCHAR(255) NOT NULL,
                        role VARCHAR(32) DEFAULT 'viewer',
                        locale VARCHAR(8) DEFAULT 'en',
                        must_change_password BOOLEAN DEFAULT 0
                    )"""
                )
                conn.commit()
        except Exception:
            pass


_ensure_users_table()

AGENT_EVENTS: list[dict] = []
AGENT_EVENTS_LOCK = Lock()
MAX_AGENT_EVENTS = 300
HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "30"))
FAILOVER_INTERVAL_SECONDS = int(os.getenv("FAILOVER_INTERVAL_SECONDS", "10"))


def add_agent_event(agent_id: str, agent_name: str, event_type: str, message: str) -> None:
    """
    Append an agent lifecycle event to the in-memory event ring buffer.

    :param agent_id: Unique identifier of the agent.
    :param agent_name: Human-readable agent name.
    :param event_type: Event category (e.g. 'discovered', 'offline').
    :param message: Descriptive message for the event.
    """
    event = {
        "id": str(uuid.uuid4()),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_id": agent_id,
        "agent_name": agent_name,
        "event_type": event_type,
        "message": message,
    }
    with AGENT_EVENTS_LOCK:
        AGENT_EVENTS.insert(0, event)
        if len(AGENT_EVENTS) > MAX_AGENT_EVENTS:
            del AGENT_EVENTS[MAX_AGENT_EVENTS:]

app = FastAPI(title="CryptoBot Manager", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
def startup_event() -> None:
    """
    Bootstrap the admin user and start the failover maintenance thread.
    """
    db = SessionLocal()
    try:
        ensure_admin_user(db)
    finally:
        db.close()
    thread = Thread(target=failover_maintenance_loop, daemon=True)
    thread.start()


def bot_to_response(bot: Bot) -> BotResponse:
    """
    Convert a Bot ORM instance to its Pydantic response schema.

    :param bot: The Bot database model instance.
    :return: A BotResponse Pydantic model.
    """
    return BotResponse(
        id=bot.id,
        name=bot.name,
        strategy_type=bot.strategy_type,
        mode=bot.mode,
        status=bot.status,
        assigned_agent_id=bot.assigned_agent_id,
        config=json.loads(bot.config_json),
        latest_metrics=json.loads(bot.latest_metrics_json or "{}"),
        created_at=bot.created_at,
        updated_at=bot.updated_at,
    )


def resolve_agent_url(agent_id: str, db: Session) -> str:
    """
    Return the base URL of an approved agent.

    :param agent_id: Unique identifier of the agent.
    :param db: Database session.
    :return: The agent's base URL string.
    :raises HTTPException: 404 if agent not found, 400 if not approved.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.approval_status != "approved":
        raise HTTPException(status_code=400, detail="Agent is not approved")
    return agent.base_url


def detach_bots_for_agent(agent: Agent, db: Session) -> None:
    """
    Stop all running bots on an agent and unassign them.

    :param agent: The Agent ORM instance whose bots to detach.
    :param db: Database session.
    """
    bots = db.query(Bot).filter(Bot.assigned_agent_id == agent.id).all()
    for bot in bots:
        if bot.status == "running":
            post_json(f"{agent.base_url}/agent/bots/{bot.id}/stop", {"bot_id": bot.id})
        bot.status = "stopped"
        bot.assigned_agent_id = None
        bot.updated_at = datetime.utcnow()


def try_failover_for_bot(bot: Bot, failed_agent: Agent, db: Session) -> bool:
    """
    Attempt to move a bot from a failed agent to another approved online agent.

    :param bot: The Bot ORM instance to fail over.
    :param failed_agent: The Agent that is no longer available.
    :param db: Database session.
    :return: True if failover succeeded, False otherwise.
    """
    target = (
        db.query(Agent)
        .filter(
            Agent.id != failed_agent.id,
            Agent.status == "online",
            Agent.approval_status == "approved",
        )
        .first()
    )
    if not target:
        return False

    cfg = json.loads(bot.config_json)
    ok, message = post_json(
        f"{target.base_url}/agent/bots/{bot.id}/start",
        {
            "bot_id": bot.id,
            "config": cfg,
        },
    )
    if not ok:
        add_agent_event(
            failed_agent.id,
            failed_agent.name,
            "failover_failed",
            f"Failover for bot {bot.name} failed: {message}",
        )
        return False

    bot.assigned_agent_id = target.id
    bot.updated_at = datetime.utcnow()
    add_agent_event(
        target.id,
        target.name,
        "failover_success",
        f"Bot {bot.name} moved from {failed_agent.name} to {target.name}.",
    )
    return True


def failover_maintenance_loop() -> None:
    """
    Background loop that monitors agent heartbeats and triggers failover.

    Runs indefinitely in a daemon thread.  Every FAILOVER_INTERVAL_SECONDS
    it checks whether any approved agent has exceeded the heartbeat timeout
    and, if so, marks it offline and attempts to migrate its running bots
    to another healthy agent.
    """
    while True:
        db = SessionLocal()
        try:
            now = datetime.utcnow()
            approved_agents = db.query(Agent).filter(Agent.approval_status == "approved").all()

            for agent in approved_agents:
                age_seconds = (now - agent.last_heartbeat).total_seconds()
                if age_seconds > HEARTBEAT_TIMEOUT_SECONDS:
                    if agent.status != "offline":
                        add_agent_event(
                            agent.id,
                            agent.name,
                            "offline",
                            f"Agent {agent.name} marked offline after heartbeat timeout.",
                        )
                    agent.status = "offline"
                elif agent.status == "offline":
                    agent.status = "online"
                    add_agent_event(
                        agent.id,
                        agent.name,
                        "recovered",
                        f"Agent {agent.name} recovered and is online again.",
                    )

            db.commit()

            offline_agents = db.query(Agent).filter(Agent.status == "offline", Agent.approval_status == "approved").all()
            for offline_agent in offline_agents:
                running_bots = (
                    db.query(Bot)
                    .filter(Bot.assigned_agent_id == offline_agent.id, Bot.status == "running")
                    .all()
                )
                for bot in running_bots:
                    try_failover_for_bot(bot, offline_agent, db)

            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()

        sleep(FAILOVER_INTERVAL_SECONDS)


@app.get("/")
def root():
    return FileResponse(static_dir / "index.html")


@app.get("/login")
def login_page():
    """
    Serve the login single-page application.

    :return: FileResponse with the login HTML page.
    """
    return FileResponse(static_dir / "login.html")


@app.get("/health")
def health():
    """
    Health check endpoint used by orchestrators and monitoring.

    :return: Dict with status, service name, and env.
    """
    return {"status": "ok", "service": "manager", "env": os.getenv("ENV", "dev")}


# ──── Auth endpoints ────

@app.post("/api/auth/login")
def auth_login(body: dict, db: Session = Depends(get_db)):
    """
    Authenticate a user by username/password and return a JWT.

    :param body: Dict with 'username' and 'password' keys.
    :param db: Database session (injected).
    :return: Dict with token and user profile.
    :raises HTTPException: 401 if credentials are invalid.
    """
    username = body.get("username", "")
    password = body.get("password", "")
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(user.id, user.role)
    return {
        "token": token,
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "locale": user.locale,
            "must_change_password": user.must_change_password,
        },
    }


@app.get("/api/auth/me")
def auth_me(user: User = Depends(get_current_user)):
    """
    Return the profile of the currently authenticated user.

    :param user: The authenticated user (injected).
    :return: Dict with user id, username, role, locale, and must_change_password.
    """
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "locale": user.locale,
        "must_change_password": user.must_change_password,
    }


@app.post("/api/auth/change-password")
def change_password(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Update the authenticated user's password and clear the forced-change flag.

    :param body: Dict with 'new_password' key.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 400 if password is shorter than 6 characters.
    """
    new_password = body.get("new_password", "")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    db.commit()
    return {"ok": True}


@app.post("/api/auth/locale")
def update_locale(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Persist the user's preferred locale (en or nl).

    :param body: Dict with 'locale' key.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with ok status and saved locale.
    :raises HTTPException: 400 if locale is unsupported.
    """
    locale = body.get("locale", "en")
    if locale not in ("en", "nl"):
        raise HTTPException(status_code=400, detail="Unsupported locale")
    user.locale = locale
    db.commit()
    return {"ok": True, "locale": locale}


# ──── User management (admin only) ────

@app.get("/api/users")
def list_users(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    List all users (admin only).

    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: List of user dicts.
    :raises HTTPException: 403 if user is not admin.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    users = db.query(User).all()
    return [
        {"id": u.id, "username": u.username, "role": u.role, "locale": u.locale, "must_change_password": u.must_change_password}
        for u in users
    ]


@app.post("/api/users")
def create_user(body: dict, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Create a new user with the given username, password, and role (admin only).

    :param body: Dict with 'username', 'password', and optional 'role' keys.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with the new user's id, username, and role.
    :raises HTTPException: 403 if not admin, 400/409 on validation errors.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    username = body.get("username", "").strip()
    password = body.get("password", "")
    role = body.get("role", "viewer")
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password required")
    if role not in ("admin", "moderator", "viewer"):
        raise HTTPException(status_code=400, detail="Invalid role")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")
    new_user = User(
        id=str(uuid.uuid4()),
        username=username,
        password_hash=hash_password(password),
        role=role,
        locale="en",
        must_change_password=True,
    )
    db.add(new_user)
    db.commit()
    return {"id": new_user.id, "username": new_user.username, "role": new_user.role}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    Delete a user by ID (admin only; cannot delete yourself).

    :param user_id: ID of the user to delete.
    :param user: The authenticated user (injected).
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 403 if not admin, 404 if user not found, 400 if self-delete.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db.delete(target)
    db.commit()
    return {"ok": True}


@app.get("/api/balance")
def get_balance(symbol: str):
    """
    Proxy a balance query to the Bitvavo REST API using HMAC authentication.

    :param symbol: The currency symbol to query (e.g. 'BTC', 'EUR').
    :return: Dict with symbol, available, and inOrder amounts.
    :raises HTTPException: 500 if credentials missing, 502 on API failure.
    """
    api_key = os.getenv("BITVAVO_API_KEY", "")
    api_secret = os.getenv("BITVAVO_API_SECRET", "")
    if not api_key or not api_secret:
        raise HTTPException(status_code=500, detail="Bitvavo API credentials not configured")

    timestamp = str(int(_time.time() * 1000))
    method = "GET"
    url_path = f"/v2/balance?symbol={symbol}"
    body = ""
    sig_string = timestamp + method + url_path + body
    signature = hmac.new(
        api_secret.encode("utf-8"),
        sig_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "BITVAVO-ACCESS-KEY": api_key,
        "BITVAVO-ACCESS-SIGNATURE": signature,
        "BITVAVO-ACCESS-TIMESTAMP": timestamp,
    }

    try:
        resp = requests.get(f"https://api.bitvavo.com{url_path}", headers=headers, timeout=6)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch balance: {exc}") from exc

    if resp.status_code == 401 or resp.status_code == 403:
        return {"symbol": symbol, "available": "0", "inOrder": "0"}

    if resp.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Bitvavo returned {resp.status_code}: {resp.text}")

    payload = resp.json()
    if isinstance(payload, list):
        if not payload:
            return {"symbol": symbol, "available": "0", "inOrder": "0"}
        entry = payload[0]
    elif isinstance(payload, dict):
        if "errorCode" in payload:
            return {"symbol": symbol, "available": "0", "inOrder": "0"}
        entry = payload
    else:
        return {"symbol": symbol, "available": "0", "inOrder": "0"}

    return {
        "symbol": entry.get("symbol", symbol),
        "available": entry.get("available", "0"),
        "inOrder": entry.get("inOrder", "0"),
    }


@app.get("/api/market/summary")
def market_summary(market: str):
    """
    Return 24h summary stats for a market from the Bitvavo REST API.

    :param market: The market pair (e.g. 'BTC-EUR').
    :return: Dict with last_price, open_24h, diff, and volume stats.
    :raises HTTPException: 502 on API failure, 404 if market not found.
    """
    try:
        response = requests.get(
            "https://api.bitvavo.com/v2/ticker/24h",
            params={"market": market},
            timeout=6,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch market data: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Bitvavo returned {response.status_code}: {response.text}")

    payload = response.json()
    if isinstance(payload, list):
        if not payload:
            raise HTTPException(status_code=404, detail="Market not found")
        data = payload[0]
    elif isinstance(payload, dict):
        data = payload
    else:
        raise HTTPException(status_code=502, detail="Unexpected market response format")

    try:
        open_price = float(data.get("open", 0.0))
        last_price = float(data.get("last", 0.0))
        volume_quote = float(data.get("volumeQuote", 0.0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"Invalid market values: {exc}") from exc

    diff_abs = last_price - open_price
    diff_pct = (diff_abs / open_price * 100.0) if open_price > 0 else 0.0

    return {
        "market": data.get("market", market),
        "last_price": last_price,
        "open_24h": open_price,
        "diff_24h_abs": diff_abs,
        "diff_24h_pct": diff_pct,
        "volume_24h_base": float(data.get("volume", 0.0) or 0.0),
        "volume_24h_quote": volume_quote,
    }


@app.get("/api/markets")
def list_markets(status: str = "trading"):
    """
    Return all Bitvavo markets filtered by status.

    :param status: Market status filter (default 'trading').
    :return: Sorted list of market dicts with market, base, quote, and status.
    :raises HTTPException: 502 on API failure.
    """
    try:
        response = requests.get("https://api.bitvavo.com/v2/markets", timeout=8)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch markets: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Bitvavo returned {response.status_code}: {response.text}")

    payload = response.json()
    if not isinstance(payload, list):
        raise HTTPException(status_code=502, detail="Unexpected markets response format")

    normalized = []
    for item in payload:
        if not isinstance(item, dict):
            continue

        market_status = str(item.get("status", "")).lower()
        if status and market_status != status.lower():
            continue

        market_symbol = item.get("market")
        if not market_symbol:
            continue

        normalized.append(
            {
                "market": market_symbol,
                "base": item.get("base"),
                "quote": item.get("quote"),
                "status": item.get("status"),
            }
        )

    normalized.sort(key=lambda x: x["market"])
    return normalized


@app.post("/api/agents/register")
def register_agent(payload: AgentRegisterRequest, db: Session = Depends(get_db)):
    """
    Register a new agent or update an existing one's connection details.

    :param payload: Agent registration data (id, name, URL, capacity).
    :param db: Database session (injected).
    :return: Dict with ok status and current approval_status.
    """
    agent = db.query(Agent).filter(Agent.id == payload.agent_id).first()
    if not agent:
        agent = Agent(
            id=payload.agent_id,
            name=payload.name,
            base_url=payload.base_url,
            capacity=payload.capacity,
            status="pending",
            approval_status="pending",
            last_heartbeat=datetime.utcnow(),
        )
        db.add(agent)
        add_agent_event(
            payload.agent_id,
            payload.name,
            "discovered",
            f"Agent {payload.name} discovered and awaiting approval.",
        )
    else:
        agent.name = payload.name
        agent.base_url = payload.base_url
        agent.capacity = payload.capacity
        if agent.approval_status == "approved":
            agent.status = "online"
        elif agent.approval_status == "rejected":
            agent.status = "rejected"
        else:
            agent.status = "pending"
        agent.last_heartbeat = datetime.utcnow()
    db.commit()
    return {"ok": True, "approval_status": agent.approval_status}


@app.post("/api/agents/{agent_id}/heartbeat")
def heartbeat(agent_id: str, payload: AgentHeartbeatRequest, db: Session = Depends(get_db)):
    """
    Process a heartbeat from an agent and update its status.

    :param agent_id: Unique identifier of the reporting agent.
    :param payload: Heartbeat data containing agent status.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.last_heartbeat = datetime.utcnow()
    if agent.approval_status == "approved":
        agent.status = payload.status
    elif agent.approval_status == "rejected":
        agent.status = "rejected"
    else:
        agent.status = "pending"
    db.commit()
    return {"ok": True}


@app.post("/api/agents/{agent_id}/approve")
def approve_agent(agent_id: str, db: Session = Depends(get_db)):
    """
    Mark an agent as approved so it can receive bot assignments.

    :param agent_id: Unique identifier of the agent to approve.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent.approval_status = "approved"
    agent.status = "online"
    agent.last_heartbeat = datetime.utcnow()
    add_agent_event(agent.id, agent.name, "approved", f"Agent {agent.name} was approved.")
    db.commit()
    return {"ok": True}


@app.post("/api/agents/{agent_id}/reject")
def reject_agent(agent_id: str, db: Session = Depends(get_db)):
    """
    Reject an agent and detach all its bots.

    :param agent_id: Unique identifier of the agent to reject.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    detach_bots_for_agent(agent, db)
    agent.approval_status = "rejected"
    agent.status = "rejected"
    agent.last_heartbeat = datetime.utcnow()
    add_agent_event(agent.id, agent.name, "rejected", f"Agent {agent.name} was rejected.")
    db.commit()
    return {"ok": True}


@app.post("/api/agents/{agent_id}/unapprove")
def unapprove_agent(agent_id: str, db: Session = Depends(get_db)):
    """
    Revoke approval for an agent and detach all its bots.

    :param agent_id: Unique identifier of the agent to un-approve.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if agent not found.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    detach_bots_for_agent(agent, db)
    agent.approval_status = "pending"
    agent.status = "pending"
    agent.last_heartbeat = datetime.utcnow()
    add_agent_event(agent.id, agent.name, "unapproved", f"Agent {agent.name} was set back to pending.")
    db.commit()
    return {"ok": True}


@app.get("/api/agents")
def list_agents(db: Session = Depends(get_db)):
    """
    Return all registered agents with their status, approval info, and bot count.

    :param db: Database session (injected).
    :return: List of agent dicts including bot_count per agent.
    """
    agents = db.query(Agent).all()
    # Count bots assigned to each agent
    bot_counts: dict[str, int] = {}
    rows = db.query(Bot.assigned_agent_id, func.count(Bot.id)).filter(
        Bot.assigned_agent_id.isnot(None),
        Bot.status == "running",
    ).group_by(Bot.assigned_agent_id).all()
    for agent_id, cnt in rows:
        bot_counts[agent_id] = cnt
    return [
        {
            "id": a.id,
            "name": a.name,
            "base_url": a.base_url,
            "status": a.status,
            "approval_status": a.approval_status,
            "capacity": a.capacity,
            "last_heartbeat": a.last_heartbeat,
            "bot_count": bot_counts.get(a.id, 0),
        }
        for a in agents
    ]


@app.get("/api/agent-events")
def list_agent_events():
    """
    Return the in-memory agent event log (most recent first).

    :return: List of agent event dicts.
    """
    with AGENT_EVENTS_LOCK:
        return list(AGENT_EVENTS)


@app.get("/api/agents/{agent_id}/logs")
def get_agent_logs(
    agent_id: str,
    limit: int = 200,
    bot_id: str | None = None,
    category: str | None = None,
    db: Session = Depends(get_db),
):
    """
    Proxy log retrieval from an approved agent, forwarding filters.

    :param agent_id: Unique identifier of the agent to query.
    :param limit: Maximum number of log entries (1-1000).
    :param bot_id: Optional filter by bot ID.
    :param category: Optional filter by log category.
    :param db: Database session (injected).
    :return: Agent's log response (proxied JSON).
    :raises HTTPException: 404 if agent not found, 400 if not approved, 502 on proxy failure.
    """
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent.approval_status != "approved":
        raise HTTPException(status_code=400, detail="Only approved agent logs are available")

    safe_limit = max(1, min(limit, 1000))
    query_params = {"limit": safe_limit}
    if bot_id:
        query_params["bot_id"] = bot_id
    if category:
        query_params["category"] = category

    try:
        response = requests.get(
            f"{agent.base_url}/agent/logs",
            params=query_params,
            timeout=6,
        )
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Failed to fetch agent logs: {exc}") from exc

    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"Agent returned {response.status_code}: {response.text}")

    return response.json()


@app.post("/api/bots", response_model=BotResponse)
def create_bot(payload: BotCreateRequest, db: Session = Depends(get_db)):
    """
    Create a new bot with the given configuration (initially stopped).

    :param payload: Bot creation request with name and config.
    :param db: Database session (injected).
    :return: BotResponse for the newly created bot.
    """
    bot_id = str(uuid.uuid4())
    bot = Bot(
        id=bot_id,
        name=payload.name,
        strategy_type=payload.config.strategy,
        mode=payload.config.mode,
        status="stopped",
        assigned_agent_id=None,
        config_json=payload.config.model_dump_json(),
        latest_metrics_json="{}",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(bot)
    db.commit()
    db.refresh(bot)
    return bot_to_response(bot)


@app.get("/api/bots", response_model=list[BotResponse])
def list_bots(db: Session = Depends(get_db)):
    """
    Return all bots with their current metrics.

    :param db: Database session (injected).
    :return: List of BotResponse models.
    """
    bots = db.query(Bot).all()
    return [bot_to_response(bot) for bot in bots]


@app.post("/api/bots/{bot_id}/start")
def start_bot(bot_id: str, payload: StartBotRequest, db: Session = Depends(get_db)):
    """
    Start a bot on a specific or auto-selected approved agent.

    :param bot_id: Unique identifier of the bot to start.
    :param payload: Request body with optional agent_id.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found, 400 if no agent available, 502 on agent failure.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    agent_id = payload.agent_id or bot.assigned_agent_id
    if not agent_id:
        agent = db.query(Agent).filter(Agent.status == "online", Agent.approval_status == "approved").first()
        if not agent:
            raise HTTPException(status_code=400, detail="No approved online agent available")
        agent_id = agent.id

    agent_url = resolve_agent_url(agent_id, db)
    ok, message = post_json(
        f"{agent_url}/agent/bots/{bot.id}/start",
        {
            "bot_id": bot.id,
            "config": json.loads(bot.config_json),
        },
    )
    if not ok:
        raise HTTPException(status_code=502, detail=f"Agent start failed: {message}")

    bot.assigned_agent_id = agent_id
    bot.status = "running"
    bot.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/bots/{bot_id}/stop")
def stop_bot(bot_id: str, db: Session = Depends(get_db)):
    """
    Stop a running bot and notify its assigned agent.

    :param bot_id: Unique identifier of the bot to stop.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found, 502 on agent failure.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if not bot.assigned_agent_id:
        bot.status = "stopped"
        db.commit()
        return {"ok": True}

    agent_url = resolve_agent_url(bot.assigned_agent_id, db)
    ok, message = post_json(f"{agent_url}/agent/bots/{bot.id}/stop", {"bot_id": bot.id})
    if not ok:
        raise HTTPException(status_code=502, detail=f"Agent stop failed: {message}")

    bot.status = "stopped"
    bot.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/bots/{bot_id}/budget")
def update_budget(bot_id: str, payload: UpdateBudgetRequest, db: Session = Depends(get_db)):
    """
    Update the budget of a bot and forward the change to its agent if running.

    :param bot_id: Unique identifier of the bot.
    :param payload: Request body with quote_budget and base_budget.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found, 502 on agent failure.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    cfg = json.loads(bot.config_json)
    cfg_budget = cfg.get("budget", {})
    cfg_budget["quote_budget"] = payload.quote_budget
    cfg_budget["base_budget"] = payload.base_budget
    cfg["budget"] = cfg_budget
    bot.config_json = json.dumps(cfg)
    bot.updated_at = datetime.utcnow()

    if bot.assigned_agent_id and bot.status == "running":
        agent_url = resolve_agent_url(bot.assigned_agent_id, db)
        ok, message = post_json(
            f"{agent_url}/agent/bots/{bot.id}/budget",
            {
                "bot_id": bot.id,
                "budget": {
                    "quote_budget": payload.quote_budget,
                    "base_budget": payload.base_budget,
                },
            },
        )
        if not ok:
            raise HTTPException(status_code=502, detail=f"Agent budget update failed: {message}")

    db.commit()
    return {"ok": True}


@app.post("/api/agents/{agent_id}/bots/{bot_id}/metrics")
def push_metrics(agent_id: str, bot_id: str, payload: MetricsPushRequest, db: Session = Depends(get_db)):
    """
    Accept a metrics snapshot from an agent for a specific bot.

    :param agent_id: Unique identifier of the reporting agent.
    :param bot_id: Unique identifier of the bot.
    :param payload: Metrics data containing a BotSnapshot.
    :param db: Database session (injected).
    :return: Dict with ok status.
    :raises HTTPException: 404 if bot not found.
    """
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    bot.latest_metrics_json = payload.snapshot.model_dump_json()
    bot.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/api/backtest", response_model=BacktestResponse)
def backtest(payload: BacktestRequest):
    """
    Run a quick backtest with the supplied configuration and price data.

    :param payload: Backtest request with config and optional prices.
    :return: BacktestResponse with equity and trade stats.
    """
    result = run_backtest(payload.config, payload.prices)
    return BacktestResponse(**result)


@app.post("/api/strategy/static-grid/preview", response_model=StaticGridPreviewResponse)
def static_grid_preview(payload: StaticGridPreviewRequest):
    """
    Return a profitability preview for the given static grid parameters.

    :param payload: Grid preview request with grid config and fee_rate.
    :return: StaticGridPreviewResponse with profitability stats.
    """
    result = build_static_grid_profit_preview(payload.grid, payload.fee_rate)
    return StaticGridPreviewResponse(**result)
