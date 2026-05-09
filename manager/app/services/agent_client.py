"""HTTP helper for the manager to communicate with agent services."""
from __future__ import annotations

import logging

import requests

from common.diagnostics import debug_log, get_correlation_id, trace_log

logger = logging.getLogger(__name__)


def post_json(url: str, payload: dict, timeout: float = 5.0) -> tuple[bool, str]:
    """
    POST payload as JSON to a URL.

    :param url: The target URL to send the request to.
    :param payload: Dictionary to serialize as JSON body.
    :param timeout: Request timeout in seconds.
    :return: Tuple of (success, response_body_or_error_message).
    """
    try:
        correlation_id = get_correlation_id()
        trace_log(
            logger,
            "manager_agent_post",
            "Manager POST to agent",
            url=url,
            timeout=timeout,
            payload=payload,
        )
        response = requests.post(
            url,
            json=payload,
            timeout=timeout,
            headers={"x-correlation-id": correlation_id},
        )
        if response.status_code >= 400:
            # Try to extract the 'detail' field from FastAPI error responses
            detail = ""
            try:
                body = response.json()
                detail = body.get("detail", "")
            except Exception:
                detail = response.text
            msg = f"{response.status_code}: {detail or response.text}"
            logger.warning("POST %s returned %s", url, msg)
            debug_log(logger, "manager_agent_post_failed", "Manager POST to agent returned error", url=url, status_code=response.status_code, detail=msg)
            return False, msg
        debug_log(logger, "manager_agent_post_ok", "Manager POST to agent succeeded", url=url, status_code=response.status_code)
        return True, response.text
    except requests.RequestException as exc:
        logger.error("POST %s failed: %s", url, exc)
        debug_log(logger, "manager_agent_post_exception", "Manager POST to agent failed with exception", url=url, error=str(exc))
        return False, str(exc)


def get_json(url: str, timeout: float = 5.0) -> tuple[bool, object]:
    """
    GET JSON from a URL.

    :param url: The target URL.
    :param timeout: Request timeout in seconds.
    :return: Tuple of (success, parsed_json_or_error_message).
    """
    try:
        correlation_id = get_correlation_id()
        trace_log(logger, "manager_agent_get", "Manager GET from agent", url=url, timeout=timeout)
        response = requests.get(url, timeout=timeout, headers={"x-correlation-id": correlation_id})
        if response.status_code >= 400:
            debug_log(logger, "manager_agent_get_failed", "Manager GET from agent returned error", url=url, status_code=response.status_code)
            return False, f"{response.status_code}: {response.text}"
        debug_log(logger, "manager_agent_get_ok", "Manager GET from agent succeeded", url=url, status_code=response.status_code)
        return True, response.json()
    except requests.RequestException as exc:
        logger.error("GET %s failed: %s", url, exc)
        debug_log(logger, "manager_agent_get_exception", "Manager GET from agent failed with exception", url=url, error=str(exc))
        return False, str(exc)
