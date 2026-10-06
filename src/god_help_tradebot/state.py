"""Persistent storage for monitoring state.

Exit plans are saved to a JSON file so a bot restart never leaves an open
position unmanaged. The store is a thin, atomic-write JSON file — no database
required for a single-machine bot.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from pathlib import Path

from .monitor import ExitPlan

DEFAULT_STATE_FILE = Path("bot_state.json")


class StateStore:
    """Load and save ExitPlans to a JSON file with atomic writes."""

    def __init__(self, path: str | Path = DEFAULT_STATE_FILE) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, ExitPlan]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        plans = data.get("exit_plans", {})
        return {
            symbol: ExitPlan(
                symbol=symbol,
                quantity=int(record["quantity"]),
                target_price=Decimal(str(record["target_price"])),
                stop_price=Decimal(str(record["stop_price"])),
            )
            for symbol, record in plans.items()
            if isinstance(record, dict)
        }

    def save(self, plans: dict[str, ExitPlan]) -> None:
        payload = {
            "exit_plans": {
                symbol: {
                    "quantity": plan.quantity,
                    "target_price": str(plan.target_price),
                    "stop_price": str(plan.stop_price),
                }
                for symbol, plan in plans.items()
            }
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self, symbol: str, plans: dict[str, ExitPlan]) -> None:
        """Drop one symbol's plan and persist the rest."""
        plans.pop(symbol, None)
        self.save(plans)


__all__ = ["StateStore", "DEFAULT_STATE_FILE"]
