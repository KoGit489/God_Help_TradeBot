"""Monitoring loop that watches open positions and manages bracket exits.

The loop stays broker-agnostic through the BrokerAdapter contract. It polls quotes,
exits positions at their planned target or stop, and can enter the top-ranked
screened candidate with an OCO-style bracket when the market is open.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable

from .broker import BrokerAdapter, WebullSandboxBroker
from .config import BotConfig
from .confirm import confirm_candidate
from .paper import OrderSide, OrderStatus, OrderType, PaperOrder, Position, Quote
from .risk import TradePlan, build_trade_plan
from .schedule import TradingSession, get_trading_session
from .screen import (
    MarketSnapshot,
    ProfiledCandidate,
    RankedCandidate,
    best_candidate,
    screen_candidates,
    screen_prepop_candidates,
)


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
        confirmation: Callable[[str], Any] | None = None,
        min_confirmation_score: float = 0.5,
        now_provider: Callable[[], "datetime"] | None = None,
        poll_seconds: float = 5.0,
        max_polls: int | None = None,
        allow_entries: bool = False,
        use_events: bool = False,
        state_store: Any | None = None,
        flatten_before_close: bool = True,
        max_consecutive_errors: int = 5,
        error_backoff_seconds: float = 30.0,
    ) -> None:
        self.broker = broker
        self.config = config or BotConfig()
        self.exit_plans = dict(exit_plans or {})
        self.snapshot_provider = snapshot_provider
        self.confirmation = confirmation
        self.min_confirmation_score = min_confirmation_score
        self._now = now_provider or _default_now
        self.poll_seconds = poll_seconds
        self.max_polls = max_polls
        self.allow_entries = allow_entries
        self.use_events = use_events
        self.state_store = state_store
        self.flatten_before_close = flatten_before_close
        self.max_consecutive_errors = max_consecutive_errors
        self.error_backoff_seconds = error_backoff_seconds
        self._events_client: Any | None = None
        self._market_news: Any | None = None
        self._consecutive_errors = 0
        # Symbols whose exit was rejected for the current session (e.g. not
        # eligible for overnight trading); retried when the session changes.
        self._exit_blocked: dict[str, str] = {}
        # Symbols already flattened at today's cutoff; prevents duplicate sells
        # while the first market order is still settling.
        self._flattened: set[str] = set()
        self._flattened_date: Any | None = None

        # Restore persisted exit plans so a restart never strands a position.
        if self.state_store is not None and not self.exit_plans:
            self.exit_plans = dict(self.state_store.load())

    def run_once(self, summary: LoopSummary | None = None) -> LoopSummary:
        """Run one poll cycle; returns the cumulative summary.

        A single cycle failure is logged and the loop keeps running (resilience).
        """
        summary = summary or LoopSummary()
        try:
            self._run_cycle(summary)
            self._consecutive_errors = 0
        except Exception as exc:
            summary.polls += 1
            self._consecutive_errors += 1
            summary.events.append(
                LoopEvent("error", "loop", f"{type(exc).__name__}: {exc} (x{self._consecutive_errors})")
            )
        return summary

    def _run_cycle(self, summary: LoopSummary) -> None:
        summary.polls += 1

        self._sync_orders()
        positions = self.broker.get_positions()
        for symbol, position in positions.items():
            if position.quantity > 0:
                summary.positions_seen[symbol] = position
                self._maybe_exit(symbol, position, summary)

        if self.flatten_before_close:
            self._maybe_flatten(positions, summary)

        if self.allow_entries and not self._has_open_position(positions):
            self._maybe_enter(summary)

        self._persist_plans()

    def run(self, summary: LoopSummary | None = None) -> LoopSummary:
        """Poll until max_polls is reached; sleeps poll_seconds between cycles."""
        summary = summary or LoopSummary()
        if self.use_events:
            self.start_event_stream()
        while self.max_polls is None or summary.polls < self.max_polls:
            self.run_once(summary)
            if self._consecutive_errors >= self.max_consecutive_errors:
                summary.events.append(
                    LoopEvent("halt", "loop", f"stopped after {self._consecutive_errors} consecutive errors")
                )
                break
            if self.max_polls is not None and summary.polls >= self.max_polls:
                break
            # Back off longer after errors to ride out transient API failures.
            sleep_for = self.error_backoff_seconds if self._consecutive_errors else self.poll_seconds
            time.sleep(sleep_for)
        return summary

    def _sync_orders(self) -> None:
        """Refresh real broker order status when the adapter supports it."""
        sync = getattr(self.broker, "sync_orders", None)
        if callable(sync):
            sync()

    def start_event_stream(self) -> None:
        """Subscribe to order-status events via gRPC when enabled and available.

        Runs the blocking subscription on a daemon thread; polling via sync_orders
        continues each cycle as the reconciliation fallback.
        """
        if not self.use_events or self._events_client is not None:
            return
        if not isinstance(self.broker, WebullSandboxBroker) or self.broker.session is None:
            return
        try:
            import threading

            from webull.trade.trade_events_client import TradeEventsClient
        except Exception:
            return

        config = self.broker.config
        client = TradeEventsClient(config.api_key, config.api_secret, "us")
        client.on_events_message = self._on_order_event
        account_id = self.broker.session.account_id
        if not account_id:
            return
        thread = threading.Thread(
            target=lambda: client.do_subscribe([account_id]),
            daemon=True,
        )
        thread.start()
        self._events_client = client

    def _on_order_event(self, event_type, subscribe_type, payload, raw_message) -> None:
        """Handle an order-status event; polling remains the reconciliation path."""
        try:
            from webull.trade.events.types import EVENT_TYPE_ORDER, ORDER_STATUS_CHANGED
        except Exception:
            return
        if event_type == EVENT_TYPE_ORDER and subscribe_type == ORDER_STATUS_CHANGED:
            # Reconcile tracked orders against the broker on the next poll cycle;
            # this event is the fast signal that something changed.
            self._sync_orders()

    def _has_open_position(self, positions: dict[str, Position]) -> bool:
        return any(position.quantity > 0 for position in positions.values())

    def _maybe_exit(self, symbol: str, position: Position, summary: LoopSummary) -> None:
        plan = self.exit_plans.get(symbol)
        if plan is None:
            return
        quote = self.broker.get_latest_quote(symbol)
        if quote is None:
            return
        if not self._can_trade_now():
            return
        session = self._exit_trading_session()
        if self._exit_blocked.get(symbol) == session:
            return  # already rejected this session; retry when the session changes

        try:
            if quote.bid >= plan.target_price:
                order = self.broker.submit_order(
                    symbol, OrderSide.SELL, position.quantity, OrderType.LIMIT,
                    limit_price=float(plan.target_price),
                    trading_session=session,
                )
                summary.exits.append(order)
                summary.events.append(
                    LoopEvent("take_profit_order", symbol, f"submitted {position.quantity} at {quote.bid}")
                )
                self._exit_blocked.pop(symbol, None)
            elif quote.bid <= plan.stop_price:
                # Webull's night session only accepts LIMIT orders; convert the stop
                # into a limit sell at the stop price outside regular hours.
                if self._market_open():
                    order = self.broker.submit_order(
                        symbol, OrderSide.SELL, position.quantity, OrderType.STOP,
                        stop_price=float(plan.stop_price),
                        trading_session="CORE",
                    )
                else:
                    order = self.broker.submit_order(
                        symbol, OrderSide.SELL, position.quantity, OrderType.LIMIT,
                        limit_price=float(plan.stop_price),
                        trading_session="NIGHT",
                    )
                summary.exits.append(order)
                summary.events.append(
                    LoopEvent("stop_loss_order", symbol, f"submitted {position.quantity} at {quote.bid}")
                )
                self._exit_blocked.pop(symbol, None)
        except Exception as exc:
            if self._is_deterministic_rejection(exc):
                self._exit_blocked[symbol] = session
                summary.events.append(
                    LoopEvent("exit_blocked", symbol, f"{session} rejected exit; will retry next session")
                )
            else:
                raise

    @staticmethod
    def _is_deterministic_rejection(exc: Exception) -> bool:
        """HTTP 417 rejections (session/eligibility/params) won't fix themselves this cycle."""
        status = getattr(exc, "http_status", None)
        if status is None:
            status = getattr(exc, "status_code", None)
        if status is None:
            return "417" in str(exc) or "OPENAPI_" in str(exc)
        return int(status) == 417

    def _can_trade_now(self) -> bool:
        """True during regular hours or the night session; false in dead zones."""
        return self._market_open() or self._in_night_session()

    def _in_night_session(self) -> bool:
        """Webull's night session runs 8 PM - 4 AM ET on trading days."""
        now = self._now()
        if get_trading_session(now.date()) is None:
            return False
        minutes = now.hour * 60 + now.minute
        return minutes >= 20 * 60 or minutes < 4 * 60

    def _exit_trading_session(self) -> str:
        return "CORE" if self._market_open() else "NIGHT"

    def _maybe_flatten(self, positions: dict[str, Position], summary: LoopSummary) -> None:
        """Close all open positions at the end-of-day cutoff (no overnight holds)."""
        if not self._at_flatten_time():
            return
        today = self._now().date()
        if self._flattened_date != today:
            self._flattened.clear()
            self._flattened_date = today
        for symbol, position in positions.items():
            if position.quantity <= 0 or symbol in self._flattened:
                continue
            order = self.broker.submit_order(
                symbol, OrderSide.SELL, position.quantity, OrderType.MARKET,
            )
            summary.exits.append(order)
            summary.events.append(
                LoopEvent("eod_flatten", symbol, f"market-sold {position.quantity} before close")
            )
            self._flattened.add(symbol)
            self.exit_plans.pop(symbol, None)

    def _at_flatten_time(self) -> bool:
        now = self._now()
        session = get_trading_session(now.date())
        return session is not None and now >= session.exit_time

    def _persist_plans(self) -> None:
        if self.state_store is not None:
            self.state_store.save(self.exit_plans)

    def _maybe_enter(self, summary: LoopSummary) -> None:
        if not self._market_open():
            return
        snapshots = self._gather_snapshots()
        if not snapshots:
            return

        chosen = self._select_candidate(snapshots, summary)
        if chosen is None:
            return
        self._enter(chosen, summary)

    def _gather_snapshots(self) -> list[MarketSnapshot]:
        """Combine momentum (top gainers) and pre-pop (most active) discovery."""
        if self.snapshot_provider is not None:
            return self.snapshot_provider()
        if not isinstance(self.broker, WebullSandboxBroker) or self.broker.session is None:
            return []
        from .autoscreen import auto_screen_snapshots

        snapshots = auto_screen_snapshots(self.broker, self.config, source="gainers")
        prepop = auto_screen_snapshots(self.broker, self.config, source="most_active")
        # Deduplicate by symbol, keeping the first occurrence.
        seen: dict[str, MarketSnapshot] = {}
        for snapshot in snapshots + prepop:
            seen.setdefault(snapshot.symbol, snapshot)
        return list(seen.values())

    def _select_candidate(
        self,
        snapshots: list[MarketSnapshot],
        summary: LoopSummary,
    ) -> tuple[ProfiledCandidate, bool] | None:
        """Pick the best candidate across profiles; fall back to best-odds if needed.

        Returns (profiled_candidate, is_fallback) or None when nothing qualifies.
        """
        for profiled in self._ranked_profiles(snapshots):
            if self._passes_confirmation(profiled.candidate.snapshot.symbol, summary):
                return profiled, False

        # Forced fallback: relax gates progressively and take the best-odds survivor.
        return self._best_odds_fallback(snapshots, summary)

    def _ranked_profiles(self, snapshots: list[MarketSnapshot]) -> list[ProfiledCandidate]:
        """Rank momentum and pre-pop candidates together, best score first."""
        combined = [
            ProfiledCandidate(candidate, "momentum")
            for candidate in screen_candidates(snapshots, self.config)
        ] + [
            ProfiledCandidate(candidate, "prepop")
            for candidate in screen_prepop_candidates(snapshots, self.config)
        ]
        combined.sort(key=lambda item: item.candidate.score, reverse=True)
        return combined

    def _best_odds_fallback(
        self,
        snapshots: list[MarketSnapshot],
        summary: LoopSummary,
    ) -> tuple[ProfiledCandidate, bool] | None:
        """Progressively relax screening gates and take the best-odds survivor."""
        relaxed = dataclasses.replace(
            self.config,
            min_change_percent=-5.0,
            max_change_percent=100.0,
            min_relative_volume=1.0,
            min_average_volume=50_000,
            max_spread_percent=3.0,
        )
        candidates = screen_candidates(snapshots, relaxed)
        if not candidates:
            return None
        best = candidates[0]
        summary.events.append(
            LoopEvent(
                "fallback_entry_candidate",
                best.snapshot.symbol,
                f"best-odds fallback (score {best.score})",
            )
        )
        return ProfiledCandidate(best, "fallback"), True

    def _enter(
        self,
        chosen: tuple[ProfiledCandidate, bool],
        summary: LoopSummary,
    ) -> None:
        profiled, is_fallback = chosen
        snapshot = profiled.candidate.snapshot
        stop_price = Decimal(str(snapshot.day_low)) * Decimal("0.99")
        try:
            plan = build_trade_plan(
                snapshot.price, float(stop_price), self.config, account_value=self._account_value()
            )
        except ValueError:
            return
        order = self.broker.submit_order(
            snapshot.symbol, OrderSide.BUY, plan.quantity, OrderType.MARKET,
        )
        summary.entries.append(order)
        summary.events.append(
            LoopEvent(
                "entry",
                snapshot.symbol,
                f"bought {plan.quantity} [{profiled.profile}] (score {profiled.candidate.score})",
            )
        )
        self.exit_plans[snapshot.symbol] = ExitPlan(
            symbol=snapshot.symbol,
            quantity=plan.quantity,
            target_price=plan.target_price,
            stop_price=plan.stop_price,
        )
        if is_fallback:
            summary.events.append(
                LoopEvent("fallback_entry", snapshot.symbol, "entered on relaxed best-odds gates")
            )

    def _first_confirmed(
        self,
        ranked: list[RankedCandidate],
        summary: LoopSummary,
    ) -> RankedCandidate | None:
        """Return the top-ranked candidate that also passes confirmation, if enabled."""
        for candidate in ranked:
            symbol = candidate.snapshot.symbol
            if self._passes_confirmation(symbol, summary):
                return candidate
        return None

    def _passes_confirmation(self, symbol: str, summary: LoopSummary) -> bool:
        if self.confirmation is None:
            return True
        report = self._confirm(symbol)
        score = getattr(report, "score", None)
        passed = getattr(report, "passed", None)
        if passed is None and score is not None:
            passed = score >= self.min_confirmation_score
        if passed is None:
            return True
        summary.events.append(
            LoopEvent(
                "confirmation",
                symbol,
                f"score {score if score is not None else 'n/a'} -> {'pass' if passed else 'reject'}",
            )
        )
        return bool(passed)

    def _confirm(self, symbol: str) -> Any:
        """Run confirmation, supplying the shared market backdrop when available."""
        if self.confirmation is confirm_candidate or self.confirmation is None:
            if isinstance(self.broker, WebullSandboxBroker):
                return confirm_candidate(
                    self.broker,
                    symbol,
                    min_score=self.min_confirmation_score,
                    market_news=self._market_news_report(),
                )
        return self.confirmation(symbol)

    def _market_news_report(self) -> Any | None:
        """Fetch the broad market backdrop once per loop run and reuse it."""
        if self._market_news is None:
            try:
                from .worldnews import fetch_market_news

                self._market_news = fetch_market_news()
            except Exception:
                self._market_news = None
        return self._market_news

    def _account_value(self) -> float | None:
        """Buying power for account-percentage sizing, when the broker supports it."""
        getter = getattr(self.broker, "get_buying_power", None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:
            return None
        return float(value) if value is not None else None

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
    confirmation: Callable[[str], Any] | None = None,
    use_events: bool = False,
) -> LoopSummary:
    """Connect the sandbox adapter and start the loop.

    With allow_entries enabled and no snapshot_provider, the loop auto-discovers
    candidates from both the top-gainers (momentum) and most-active (pre-pop)
    lists, picks the higher-odds candidate, and falls back to best-odds entry if
    nothing passes the normal gates.
    """
    broker = WebullSandboxBroker.from_env().connect()
    loop = MonitorLoop(
        broker,
        allow_entries=allow_entries,
        snapshot_provider=snapshot_provider,
        confirmation=confirmation,
        poll_seconds=poll_seconds,
        max_polls=max_polls,
        use_events=use_events,
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
