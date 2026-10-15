"""Session recorder: captures the bot's live decision data for later study.

Appends one JSON line per poll cycle to data/YYYY-MM-DD.jsonl with the time,
the candidate snapshots the loop saw, and the quotes in play. Those files feed
the weekly study and the backtest harness — same data the bot decided on.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .paper import Quote
from .screen import MarketSnapshot

DEFAULT_DATA_DIR = Path("data")


class SessionRecorder:
    """Append-only recorder writing one JSONL step per poll cycle."""

    def __init__(self, data_dir: str | Path = DEFAULT_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)

    def _path_for(self, when: datetime) -> Path:
        return self.data_dir / f"{when.date().isoformat()}.jsonl"

    def record_step(
        self,
        when: datetime,
        snapshots: list[MarketSnapshot],
        quotes: list[Quote],
    ) -> None:
        """Append one cycle's data to that day's timeline file."""
        if not snapshots and not quotes:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        step = {
            "time": when.isoformat(),
            "snapshots": [self._snapshot_to_dict(s) for s in snapshots],
            "quotes": [self._quote_to_dict(q) for q in quotes],
        }
        with self._path_for(when).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(step) + "\n")

    @staticmethod
    def _snapshot_to_dict(snapshot: MarketSnapshot) -> dict[str, Any]:
        return {
            "symbol": snapshot.symbol,
            "price": snapshot.price,
            "change_percent": snapshot.change_percent,
            "volume": snapshot.volume,
            "average_volume": snapshot.average_volume,
            "bid": snapshot.bid,
            "ask": snapshot.ask,
            "day_low": snapshot.day_low,
            "day_high": snapshot.day_high,
        }

    @staticmethod
    def _quote_to_dict(quote: Quote) -> dict[str, Any]:
        return {
            "symbol": quote.symbol,
            "bid": float(quote.bid),
            "ask": float(quote.ask),
            "last": float(quote.last),
        }


def load_recorded_day(path: str | Path) -> list[dict[str, Any]]:
    """Load a recorded day file (JSONL, one step per line) into a backtest timeline."""
    steps: list[dict[str, Any]] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            steps.append(json.loads(line))
    return steps


def list_recorded_days(data_dir: str | Path = DEFAULT_DATA_DIR) -> list[Path]:
    """All recorded day files, oldest first."""
    directory = Path(data_dir)
    if not directory.exists():
        return []
    return sorted(directory.glob("*.jsonl"))


__all__ = ["SessionRecorder", "DEFAULT_DATA_DIR", "list_recorded_days", "load_recorded_day"]
