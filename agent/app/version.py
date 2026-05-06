"""Version information for the CryptoBot Agent."""
from __future__ import annotations

import tomllib
from pathlib import Path

_pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
with _pyproject.open("rb") as _f:
    __version__: str = tomllib.load(_f)["project"]["version"]
