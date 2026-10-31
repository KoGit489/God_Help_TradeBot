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
    "strategy_mode": str,
    "min_symbol_price": float,
    "max_symbol_price": float,
    "risk_per_trade_usd": float,
    "reward_multiple": float,
    "max_position_value_usd": float,
    "min_change_percent": float,
    "max_change_percent": float,
    "min_relative_volume": float,
    "min_average_volume": int,
    "max_spread_percent": float,
    "position_pct_of_account": float,
    "allow_shorts": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"),
    "stop_pct": float,
    "target_pct": float,
    "min_signals_present": int,
    "require_trend_alignment": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"),
    "allow_fallback": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"),
    "entry_cooldown_minutes": int,
    "require_double_confirmation": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"),
    "momentum_max_change_percent": float,
    "entry_limit_spread_fraction": float,
    "entry_chase_polls": int,
    "exit_chase_polls": int,
    "prepop_fast_trigger": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"),
    "paradox_fallback": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"),
    "fallback_after_minutes": int,
    "paradox_trend_guard": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"),
    "screener_list_limit": int,
    "max_confirmations_per_cycle": int,
    "study_interval_days": int,
    "study_target_winning_days": int,
    "study_auto_improve": lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on"),
    "spy_option_initial_budget_usd": float,
    "spy_option_buying_power_fraction": float,
    "spy_option_max_spread_percent": float,
    "spy_option_weak_max_spread_percent": float,
    "spy_option_min_abs_delta": float,
    "spy_option_max_abs_delta": float,
    "spy_option_strong_score": float,
    "spy_option_weak_floor": float,
    "spy_option_min_direction_gap": float,
    "spy_option_strong_direction_gap": float,
    "spy_option_max_strike_distance": float,
    "spy_option_min_signal_count": int,
    "spy_option_max_quote_delay_minutes": int,
    "spy_option_max_contracts_per_order": int,
    "spy_option_scan_minutes_before_open": int,
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
                if "spy_option_contract_budget_usd" in data:
                    overrides.setdefault(
                        "spy_option_initial_budget_usd",
                        float(data["spy_option_contract_budget_usd"]),
                    )
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            pass  # A malformed file falls back to defaults rather than crashing

    # Environment variables override the file (BOT_MAX_SYMBOL_PRICE=5, etc.).
    legacy_budget = os.environ.get("BOT_SPY_OPTION_CONTRACT_BUDGET_USD")
    if legacy_budget is not None and "BOT_SPY_OPTION_INITIAL_BUDGET_USD" not in os.environ:
        try:
            overrides["spy_option_initial_budget_usd"] = float(legacy_budget)
        except ValueError:
            pass
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
