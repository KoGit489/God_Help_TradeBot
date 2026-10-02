from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from .broker import WebullSessionConfig


def _load_dotenv_file(env_file: str | Path | None = None) -> dict[str, str]:
    path = Path(env_file) if env_file else Path.cwd() / ".env"
    values: dict[str, str] = {}

    if not path.exists():
        return values

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")

    return values


def load_webull_config(
    *,
    env: Mapping[str, str] | None = None,
    env_file: str | Path | None = None,
) -> WebullSessionConfig:
    """Load Webull credentials from the environment and optional .env file.

    Values in the process environment override the .env file for the same keys.
    """
    source = os.environ if env is None else dict(env)
    dotenv_values = _load_dotenv_file(env_file)
    merged = {**dotenv_values, **source}

    return WebullSessionConfig(
        api_key=merged.get("WEBULL_API_KEY"),
        api_secret=merged.get("WEBULL_API_SECRET"),
        account_id=merged.get("WEBULL_ACCOUNT_ID"),
        sandbox_mode=True,
        live_mode=False,
    )


__all__ = ["load_webull_config"]
