"""Monitoring loop that watches open positions and manages bracket exits.

The loop stays broker-agnostic through the BrokerAdapter contract. It polls quotes,
exits positions at their planned target or stop, and can enter the top-ranked
screened candidate with an OCO-style bracket when the market is open.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from .broker import BrokerAdapter, WebullSandboxBroker
from .config import BotConfig
from .paper import OrderSide, OrderStatus, OrderType, PaperOrder, Position, Quote
from .risk import TradePlan, build_trade_plan
from .schedule import TradingSession, get_trading_session
from .screen import MarketSnapshot, RankedCandidate, screen_candidates


@dataclass(frozen=True)
class ExitPlan:
    """Price levels and sizing that define one position's exit bracket."""

    symbol: str
    quantity: int
    target_price: Decimal
    stop_price: Decimal


@dataclass(frozen=True)
class LoopEvent:
    """One notable action taken during a poll cycle."""

    kind: str
    symbol: str
    detail: str


@dataclass
class LoopSummary:
    """Aggregate result of a monitoring run."""

    polls: int = 0
    events: list[LoopEvent] = field(default_factory=list)
    positions_seen: dict[str, Position] = field(default_factory=dict)
    entries: list[PaperOrder] = field(default_factory=list)
    exits: list[PaperOrder] = field(default_factory=list)


class MonitorLoop:
    """Poll-driven position monitor and bracket-exit manager."""

    def __init__(
        self,
        broker: BrokerAdapter,
        config: BotConfig | None = None,
        *,
        exit_plans: dict[str, ExitPlan] | None = None,
        snapshot_provider: Callable[[], list[MarketSnapshot]] | None = None,
        now_provider: Callable[[], "datetime"] | None = None,
        poll_seconds: float = 5.0,
        max_polls: int | None = None,
        allow_entries: bool = False,
    ) -> None:
        self.broker = broker
        self.config = config or BotConfig()
        self.exit_plans = dict(exit_plans or {})
        self.snapshot_provider = snapshot_provider
        self._now = now_provider or _default_now
        self.poll_seconds = poll_seconds
        self.max_polls = max_polls
        self.allow_entries = allow_entries

    def run_once(self, summary: LoopSummary | None = None) -> LoopSummary:
        """Run one poll cycle; returns the cumulative summary."""
        summary = summary or LoopSummary()
        summary.polls += 1

        positions = self.broker.get_positions()
        for symbol, position in positions.items():
            if position.quantity > 0:
                summary.positions_seen[symbol] = position
                self._maybe_exit(symbol, position, summary)

        if self.allow_entries and not self._has_open_position(positions):
            self._maybe_enter(summary)

        return summary

    def run(self, summary: LoopSummary | None = None) -> LoopSummary:
        """Poll until max_polls is reached; sleeps poll_seconds between cycles."""
        summary = summary or LoopSummary()
        while self.max_polls is None or summary.polls < self.max_polls:
            self.run_once(summary)
            if self.max_polls is not None and summary.polls >= self.max_polls:
                break
            time.sleep(self.poll_seconds)
        return summary

    def _has_open_position(self, positions: dict[str, Position]) -> bool:
        return any(position.quantity > 0 for position in positions.values())

    def _maybe_exit(self, symbol: str, position: Position, summary: LoopSummary) -> None:
        plan = self.exit_plans.get(symbol)
        if plan is None:
            return
        quote = self.broker.get_latest_quote(symbol)
        if quote is None:
            return

        if quote.bid >= plan.target_price:
            order = self.broker.submit_order(
                symbol, OrderSide.SELL, position.quantity, OrderType.LIMIT,
                limit_price=float(plan.target_price),
            )
            order.status = OrderStatus.FILLED
            order.fill_price = quote.bid
            summary.exits.append(order)
            summary.events.append(
                LoopEvent("take_profit", symbol, f"sold {position.quantity} at {quote.bid}")
            )
        elif quote.bid <= plan.stop_price:
            order = self.broker.submit_order(
                symbol, OrderSide.SELL, position.quantity, OrderType.STOP,
                stop_price=float(plan.stop_price),
            )
            order.status = OrderStatus.FILLED
            order.fill_price = quote.bid
            summary.exits.append(order)
            summary.events.append(
                LoopEvent("stop_loss", symbol, f"sold {position.quantity} at {quote.bid}")
            )

    def _maybe_enter(self, summary: LoopSummary) -> None:
        provider = self.snapshot_provider or self._auto_snapshots
        if not self._market_open():
            return
        ranked = screen_candidates(provider(), self.config)
        if not ranked:
            return
        candidate = ranked[0]
        snapshot = candidate.snapshot
        stop_price = Decimal(str(snapshot.day_low)) * Decimal("0.99")
        try:
            plan = build_trade_plan(snapshot.price, float(stop_price), self.config)
        except ValueError:
            return
        order = self.broker.submit_order(
            snapshot.symbol, OrderSide.BUY, plan.quantity, OrderType.MARKET,
        )
        summary.entries.append(order)
        summary.events.append(
            LoopEvent("entry", snapshot.symbol, f"bought {plan.quantity} (score {candidate.score})")
        )
        self.exit_plans[snapshot.symbol] = ExitPlan(
            symbol=snapshot.symbol,
            quantity=plan.quantity,
            target_price=plan.target_price,
            stop_price=plan.stop_price,
        )

    def _auto_snapshots(self) -> list[MarketSnapshot]:
        """Discover candidates automatically when the broker is a Webull sandbox session."""
        if not isinstance(self.broker, WebullSandboxBroker) or self.broker.session is None:
            return []
        from .autoscreen import auto_screen_snapshots

        return auto_screen_snapshots(self.broker, self.config)

    def _market_open(self) -> bool:
        now = self._now()
        session = get_trading_session(now.date())
        return session is not None and session.market_open <= now < session.exit_time


def _default_now() -> "datetime":
    from datetime import datetime

    from .schedule import EASTERN

    return datetime.now(EASTERN)


def build_exit_plan(plan: TradePlan, symbol: str) -> ExitPlan:
    """Convert a risk TradePlan into the loop's ExitPlan."""
    return ExitPlan(
        symbol=symbol,
        quantity=plan.quantity,
        target_price=plan.target_price,
        stop_price=plan.stop_price,
    )


def run_sandbox_monitor(
    *,
    poll_seconds: float = 5.0,
    max_polls: int | None = None,
    allow_entries: bool = False,
    snapshot_provider: Callable[[], list[MarketSnapshot]] | None = None,
    auto_screen: bool = True,
) -> LoopSummary:
    """Connect the sandbox adapter and start the loop.

    By default the loop auto-screens the Webull top-gainers list for entries when
    `allow_entries` is enabled. Pass `snapshot_provider` to supply your own list,
    or set `auto_screen=False` to disable automatic discovery.
    """
    broker = WebullSandboxBroker.from_env().connect()
    provider = snapshot_provider
    if provider is None and auto_screen and allow_entries:
        from .autoscreen import auto_screen_snapshots

        provider = lambda: auto_screen_snapshots(broker)
    loop = MonitorLoop(
        broker,
        allow_entries=allow_entries,
        snapshot_provider=provider,
        poll_seconds=poll_seconds,
        max_polls=max_polls,
    )
    return loop.run()


__all__ = [
    "ExitPlan",
    "LoopEvent",
    "LoopSummary",
    "MonitorLoop",
    "build_exit_plan",
    "run_sandbox_monitor",
]
