from __future__ import annotations

import requests


def post_json(url: str, payload: dict, timeout: float = 5.0) -> tuple[bool, str]:
    try:
        response = requests.post(url, json=payload, timeout=timeout)
        if response.status_code >= 400:
            return False, f"{response.status_code}: {response.text}"
        return True, response.text
    except requests.RequestException as exc:
        return False, str(exc)
