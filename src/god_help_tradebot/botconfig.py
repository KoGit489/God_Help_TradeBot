"""Optional bot-config file loading.

Reads guardrail overrides from a JSON file (default `bot_config.json`) so you can
tune risk and screening thresholds without editing code. Missing file or keys
fall back to BotConfig defaults.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .config import BotConfig

DEFAULT_CONFIG_FILE = Path("bot_config.json")

# Keys in the JSON file that map onto BotConfig fields.
_TUNABLES = {
    "max_symbol_price": float,
    "risk_per_trade_usd": float,
    "reward_multiple": float,
    "max_position_value_usd": float,
    "min_change_percent": float,
    "max_change_percent": float,
    "min_relative_volume": float,
    "min_average_volume": int,
    "max_spread_percent": float,
}


def load_bot_config(path: str | Path = DEFAULT_CONFIG_FILE) -> BotConfig:
    """Load BotConfig, applying JSON overrides and BOT_* environment variables."""
    overrides: dict[str, object] = {}
    file_path = Path(path)
    if file_path.exists():
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for key, caster in _TUNABLES.items():
                    if key in data:
                        overrides[key] = caster(data[key])
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass  # A malformed file falls back to defaults rather than crashing

    # Environment variables override the file (BOT_MAX_SYMBOL_PRICE=5, etc.).
    for key, caster in _TUNABLES.items():
        env_value = os.environ.get(f"BOT_{key.upper()}")
        if env_value is not None:
            try:
                overrides[key] = caster(env_value)
            except (ValueError, TypeError):
                continue

    if not overrides:
        return BotConfig()
    base = BotConfig()
    values = {field: getattr(base, field) for field in _TUNABLES}
    values.update(overrides)
    return BotConfig(**values)


__all__ = ["load_bot_config", "DEFAULT_CONFIG_FILE"]
