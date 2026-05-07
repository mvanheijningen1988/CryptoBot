"""Agent failover monitoring and bot migration logic.

Runs a background loop that checks agent heartbeats and migrates
bots away from offline agents to healthy ones.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from time import sleep
from typing import TYPE_CHECKING

from manager.app.events import add_agent_event
from manager.app.models import Agent, Bot
from manager.app.services.agent_client import get_json, post_json

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

HEARTBEAT_TIMEOUT_SECONDS = int(os.getenv("HEARTBEAT_TIMEOUT_SECONDS", "30"))
FAILOVER_INTERVAL_SECONDS = int(os.getenv("FAILOVER_INTERVAL_SECONDS", "10"))


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
        bot.updated_at = datetime.now(UTC)


def try_failover_for_bot(bot: Bot, failed_agent: Agent, db: Session) -> bool:
    """
    Attempt to move a bot from a failed agent to another approved online agent.

    :param bot: The Bot ORM instance to fail over.
    :param failed_agent: The Agent that is no longer available.
    :param db: Database session.
    :return: True if failover succeeded, False otherwise.
    """
    from sqlalchemy import func

    # Find the least-loaded available agent
    agents = (
        db.query(Agent)
        .filter(
            Agent.id != failed_agent.id,
            Agent.status == "online",
            Agent.approval_status == "approved",
        )
        .all()
    )
    if not agents:
        return False

    counts = dict(
        db.query(Bot.assigned_agent_id, func.count(Bot.id))
        .filter(Bot.status == "running", Bot.assigned_agent_id.isnot(None))
        .group_by(Bot.assigned_agent_id)
        .all()
    )

    target: Agent | None = None
    best_count = float("inf")
    for agent in agents:
        n = counts.get(agent.id, 0)
        if n >= agent.capacity:
            continue
        if n < best_count or (n == best_count and target and agent.id < target.id):
            target = agent
            best_count = n

    if not target:
        return False

    cfg = json.loads(bot.config_json)
    payload: dict = {
        "bot_id": bot.id,
        "config": cfg,
    }
    # Include saved runner state so the new agent can resume
    saved_state = bot.state_json if hasattr(bot, "state_json") else "{}"
    if saved_state and saved_state != "{}":
        payload["runner_state"] = json.loads(saved_state)

    ok, message = post_json(
        f"{target.base_url}/agent/bots/{bot.id}/start",
        payload,
    )
    if not ok:
        add_agent_event(
            failed_agent.id,
            "failover_failed",
            f"Failover for bot {bot.name} failed: {message}",
        )
        return False

    bot.assigned_agent_id = target.id
    bot.updated_at = datetime.now(UTC)
    add_agent_event(
        target.id,
        "failover_success",
        f"Bot {bot.name} moved from {failed_agent.id} to {target.id}.",
    )
    return True


def _try_reassign_bot(bot: Bot, db: Session) -> bool:
    """Try to start an orphaned/queued bot on any available agent.

    :param bot: Bot that needs reassignment.
    :param db: Database session.
    :return: True if successfully reassigned.
    """
    from sqlalchemy import func

    agents = (
        db.query(Agent)
        .filter(Agent.status == "online", Agent.approval_status == "approved")
        .all()
    )
    if not agents:
        return False

    counts = dict(
        db.query(Bot.assigned_agent_id, func.count(Bot.id))
        .filter(Bot.status == "running", Bot.assigned_agent_id.isnot(None))
        .group_by(Bot.assigned_agent_id)
        .all()
    )

    target: Agent | None = None
    best_count = float("inf")
    for agent in agents:
        n = counts.get(agent.id, 0)
        if n >= agent.capacity:
            continue
        if n < best_count or (n == best_count and target and agent.id < target.id):
            target = agent
            best_count = n

    if not target:
        return False

    cfg = json.loads(bot.config_json)
    payload: dict = {"bot_id": bot.id, "config": cfg}
    saved_state = bot.state_json if hasattr(bot, "state_json") else "{}"
    if saved_state and saved_state != "{}":
        payload["runner_state"] = json.loads(saved_state)

    ok, message = post_json(f"{target.base_url}/agent/bots/{bot.id}/start", payload)
    if not ok:
        logger.warning("Reassign bot %s to %s failed: %s", bot.name, target.id, message)
        return False

    bot.assigned_agent_id = target.id
    bot.status = "running"
    bot.updated_at = datetime.now(UTC)
    add_agent_event(
        target.id,
        "bot_reassigned",
        f"Bot {bot.name} reassigned to {target.id}.",
    )
    return True


def verify_running_bots(db: Session) -> None:
    """Check that bots marked 'running' are actually running on their agent.

    If the agent doesn't have the bot, the bot is set to 'queued' and
    the manager will attempt to move it to another available agent.
    """
    running_bots = db.query(Bot).filter(Bot.status == "running").all()
    for bot in running_bots:
        if not bot.assigned_agent_id:
            # Running but no agent — queue it
            bot.status = "queued"
            bot.updated_at = datetime.now(UTC)
            logger.warning("Bot %s running with no agent — queued", bot.name)
            continue

        agent = db.query(Agent).filter(Agent.id == bot.assigned_agent_id).first()
        if not agent:
            # Agent record gone (e.g. re-registered with new ID) — queue the bot
            bot.status = "queued"
            bot.assigned_agent_id = None
            bot.updated_at = datetime.now(UTC)
            logger.warning("Bot %s assigned to unknown agent %s — queued", bot.name, bot.assigned_agent_id)
            continue
        if agent.status != "online":
            continue  # Handled by heartbeat failover logic

        # Ask the agent if it actually has this bot running
        ok, data = get_json(f"{agent.base_url}/agent/bots")
        if not ok:
            continue  # Agent unreachable — heartbeat will catch it

        agent_bot_ids = {b["bot_id"] for b in data if b.get("running")}
        if bot.id not in agent_bot_ids:
            logger.warning(
                "Bot %s marked running on agent %s but agent doesn't have it — queuing",
                bot.name, agent.id,
            )
            bot.status = "queued"
            bot.assigned_agent_id = None
            bot.updated_at = datetime.now(UTC)

    db.commit()

    # Try to reassign queued bots
    queued_bots = db.query(Bot).filter(Bot.status == "queued").all()
    for bot in queued_bots:
        if _try_reassign_bot(bot, db):
            logger.info("Queued bot %s reassigned successfully", bot.name)
    db.commit()


def failover_maintenance_loop(session_factory: sessionmaker) -> None:
    """
    Background loop that monitors agent heartbeats and triggers failover.

    Runs indefinitely in a daemon thread.  Every FAILOVER_INTERVAL_SECONDS
    it checks whether any approved agent has exceeded the heartbeat timeout
    and, if so, marks it offline and attempts to migrate its running bots
    to another healthy agent.

    :param session_factory: Callable that creates new database sessions.
    """
    while True:
        db = session_factory()
        try:
            now = datetime.now(UTC)
            approved_agents = db.query(Agent).filter(Agent.approval_status == "approved").all()

            for agent in approved_agents:
                if agent.status == "stopped":
                    continue
                hb = agent.last_heartbeat
                if hb and hb.tzinfo is None:
                    hb = hb.replace(tzinfo=UTC)
                age_seconds = (now - hb).total_seconds() if hb else HEARTBEAT_TIMEOUT_SECONDS + 1
                if age_seconds > HEARTBEAT_TIMEOUT_SECONDS:
                    if agent.status != "offline":
                        add_agent_event(
                            agent.id,
                            "offline",
                            f"Agent {agent.id} marked offline after heartbeat timeout.",
                        )
                    agent.status = "offline"
                elif agent.status == "offline":
                    agent.status = "online"
                    add_agent_event(
                        agent.id,
                        "recovered",
                        f"Agent {agent.id} recovered and is online again.",
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
                    if not try_failover_for_bot(bot, offline_agent, db):
                        # No target agent available — queue the bot for later
                        bot.status = "queued"
                        bot.assigned_agent_id = None
                        bot.updated_at = datetime.now(UTC)
                        add_agent_event(
                            offline_agent.id,
                            "failover_queued",
                            f"Bot {bot.name} queued: no agents available for failover.",
                        )

            db.commit()

            # Verify running bots are actually on their agents and
            # attempt to reassign any queued bots.
            verify_running_bots(db)
        except Exception:
            db.rollback()
        finally:
            db.close()

        sleep(FAILOVER_INTERVAL_SECONDS)
