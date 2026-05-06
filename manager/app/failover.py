"""Agent failover monitoring and bot migration logic.

Runs a background loop that checks agent heartbeats and migrates
bots away from offline agents to healthy ones.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from time import sleep
from typing import TYPE_CHECKING

from manager.app.events import add_agent_event
from manager.app.models import Agent, Bot
from manager.app.services.agent_client import post_json

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

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
    bot.updated_at = datetime.now(UTC)
    add_agent_event(
        target.id,
        target.name,
        "failover_success",
        f"Bot {bot.name} moved from {failed_agent.name} to {target.name}.",
    )
    return True


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
