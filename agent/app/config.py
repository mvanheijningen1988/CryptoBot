"""Agent-level configuration loaded from environment variables."""
from __future__ import annotations

import os
import socket
import time
from urllib.parse import urlparse
import uuid

from agent.app.runner import RunnerManager


def _detect_agent_host() -> str:
	"""Return the best host/IP for manager callbacks.

	Preference order:
	1. explicit ``AGENT_HOST`` env var
	2. non-loopback addresses from local hostname lookup
	3. fallback to ``127.0.0.1``
	"""
	explicit_host = os.getenv("AGENT_HOST", "").strip()
	if explicit_host:
		return explicit_host

	try:
		infos = socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
		for info in infos:
			ip = info[4][0]
			if ip and not ip.startswith("127."):
				return ip
	except OSError:
		pass

	return "127.0.0.1"


def _resolve_agent_base_url() -> str:
	"""Resolve agent callback URL with support for legacy placeholders."""
	configured = os.getenv("AGENT_BASE_URL", "").strip()
	if configured:
		try:
			parsed = urlparse(configured)
			if parsed.hostname and parsed.hostname.lower() != "agent":
				return configured
		except ValueError:
			return configured

	return f"http://{_detect_agent_host()}:{AGENT_PORT}"

MANAGER_URL: str = os.getenv("MANAGER_URL", "http://manager:8000")
AGENT_PORT: int = int(os.getenv("AGENT_PORT", "8100"))
AGENT_ID: str = os.getenv("AGENT_ID", str(uuid.uuid4()))
AGENT_BASE_URL: str = _resolve_agent_base_url()
AGENT_START_TIME: float = time.time()

runner_manager = RunnerManager(MANAGER_URL, AGENT_ID)
