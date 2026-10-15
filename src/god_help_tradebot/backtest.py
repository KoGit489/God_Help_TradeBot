"""Backtest harness: replay recorded market data through the real MonitorLoop.

Runs the actual screening/confirmation/entry/exit machinery against a timeline
of snapshots and quotes using PaperBroker, and reports win rate and P&L. This
measures the strategy instead of guessing at it.

Timeline input is JSON:
{
  "steps": [
    {
      "time": "2026-10-07T13:45:00-04:00",
      "snapshots": [ {MarketSnapshot fields...} ],
      "quotes": [ {"symbol": "...", "bid": 1.0, "ask": 1.01, "last": 1.0} ]
    }
  ]
}
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .config import BotConfig
from .monitor import LoopSummary, MonitorLoop
from .paper import OrderSide, PaperBroker, Position, Quote
from .screen import MarketSnapshot


@dataclass(frozen=True)
class BacktestTrade:
    symbol: str
    side: str
    quantity: int
    entry_price: Decimal
    exit_price: Decimal
    pnl: Decimal


@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    realized_pnl: Decimal = Decimal("0")
    trades: list[BacktestTrade] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.total_trades if self.total_trades else 0.0


def _snapshot_from(data: dict[str, Any]) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=str(data["symbol"]),
        price=float(data["price"]),
        change_percent=float(data["change_percent"]),
        volume=int(data["volume"]),
        average_volume=int(data["average_volume"]),
        bid=float(data["bid"]),
        ask=float(data["ask"]),
        day_low=float(data["day_low"]),
        day_high=float(data["day_high"]),
    )


def _quote_from(data: dict[str, Any]) -> Quote:
    return Quote(
        str(data["symbol"]).upper(),
        Decimal(str(data["bid"])),
        Decimal(str(data["ask"])),
        Decimal(str(data["last"])),
    )


def load_timeline(path: str | Path) -> list[dict[str, Any]]:
    """Load a backtest timeline from JSON."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    steps = payload.get("steps", payload if isinstance(payload, list) else [])
    if not isinstance(steps, list):
        raise ValueError("timeline must contain a 'steps' list")
    return steps


def run_backtest(
    timeline: list[dict[str, Any]],
    config: BotConfig | None = None,
    *,
    starting_cash: float = 100_000,
    confirmation: Callable[[str], Any] | None = None,
    allow_shorts: bool | None = None,
) -> BacktestResult:
    """Replay a timeline through the real MonitorLoop with a PaperBroker.

    Each step advances the clock, feeds the step's quotes into the paper broker
    (filling resting orders), then runs one poll cycle.
    """
    config = config or BotConfig()
    broker = PaperBroker(starting_cash)
    state: dict[str, Any] = {"now": None}

    loop = MonitorLoop(
        broker,
        config,
        confirmation=confirmation,
        now_provider=lambda: state["now"],
        allow_entries=True,
        allow_shorts=allow_shorts,
        poll_seconds=0,
        flatten_before_close=True,
    )

    result = BacktestResult()
    summary = LoopSummary()
    entry_fills: dict[str, tuple[str, Decimal, int]] = {}
    entry_order_ids: set[int] = set()
    exit_order_ids: set[int] = set()

    for step in timeline:
        state["now"] = datetime.fromisoformat(str(step["time"]))

        quotes = [q for q in (_quote_from(raw) for raw in step.get("quotes", []))]
        snapshots = [_snapshot_from(raw) for raw in step.get("snapshots", [])]
        loop.snapshot_provider = lambda s=snapshots: s

        # Feed quotes so resting orders (entries, exits) fill like live trading.
        for quote in quotes:
            broker.process_quote(quote)

        loop.run_once(summary)

        # Record fills that happened by this step.
        for order in broker.orders.values():
            if order.status.value != "FILLED" or order.fill_price is None:
                continue
            if order.order_id in entry_order_ids or order.order_id in exit_order_ids:
                continue
            if order.side in (OrderSide.BUY, OrderSide.SHORT) and order.symbol not in entry_fills:
                entry_fills[order.symbol] = (order.side.value, order.fill_price, order.quantity)
                entry_order_ids.add(order.order_id)
            elif order.symbol in entry_fills:
                entry_side, entry_price, quantity = entry_fills.pop(order.symbol)
                if entry_side == "BUY":
                    pnl = (order.fill_price - entry_price) * quantity
                    side = "long"
                else:
                    pnl = (entry_price - order.fill_price) * quantity
                    side = "short"
                result.trades.append(
                    BacktestTrade(order.symbol, side, quantity, entry_price, order.fill_price, pnl)
                )
                result.total_trades += 1
                result.realized_pnl += pnl
                if pnl > 0:
                    result.wins += 1
                else:
                    result.losses += 1
                exit_order_ids.add(order.order_id)

    return result


__all__ = ["BacktestResult", "BacktestTrade", "load_timeline", "run_backtest"]
