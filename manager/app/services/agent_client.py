"""HTTP helper for the manager to communicate with agent services."""
from __future__ import annotations

import requests


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
            return False, f"{response.status_code}: {response.text}"
        return True, response.text
    except requests.RequestException as exc:
        return False, str(exc)
