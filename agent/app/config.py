"""Agent-level configuration loaded from environment variables."""
from __future__ import annotations

import os
import time
import uuid

from agent.app.runner import RunnerManager

MANAGER_URL: str = os.getenv("MANAGER_URL", "http://manager:8000")
AGENT_PORT: int = int(os.getenv("AGENT_PORT", "8100"))
AGENT_ID: str = os.getenv("AGENT_ID", str(uuid.uuid4()))
AGENT_BASE_URL: str = os.getenv("AGENT_BASE_URL", f"http://agent:{AGENT_PORT}")
AGENT_START_TIME: float = time.time()

runner_manager = RunnerManager(MANAGER_URL, AGENT_ID)
