"""HTTP helper for the manager to communicate with agent services."""
from __future__ import annotations

import logging

import requests

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
        response = requests.post(url, json=payload, timeout=timeout)
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
            return False, msg
        return True, response.text
    except requests.RequestException as exc:
        logger.error("POST %s failed: %s", url, exc)
        return False, str(exc)
