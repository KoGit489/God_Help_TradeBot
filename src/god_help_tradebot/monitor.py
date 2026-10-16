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
from .confirm import confirm_candidate, confirm_candidate_bearish
from .paper import OrderSide, OrderStatus, OrderType, PaperOrder, Position, Quote
from .risk import TradePlan, build_short_plan, build_trade_plan
from .schedule import TradingSession, get_trading_session
from .screen import (
    MarketSnapshot,
    ProfiledCandidate,
    RankedCandidate,
    best_candidate,
    screen_candidates,
    screen_fade_candidates,
    screen_loser_candidates,
    screen_prepop_candidates,
)


@dataclass(frozen=True)
class ExitPlan:
    """Price levels and sizing that define one position's exit bracket."""

    symbol: str
    quantity: int
    target_price: Decimal
    stop_price: Decimal
    direction: str = "long"


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
        allow_shorts: bool | None = None,
        recorder: Any | None = None,
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
        self.allow_shorts = self.config.allow_shorts if allow_shorts is None else allow_shorts
        self.recorder = recorder
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
        # Candidates whose entry was rejected today (e.g. platform limits); the
        # loop moves to the next candidate instead of retrying the same one.
        self._entry_blocked: set[str] = set()
        self._entry_blocked_date: Any | None = None
        # Symbol with an entry order in flight (submitted but not yet visible as
        # a position); prevents double-entering during the settlement gap.
        self._pending_entry: str | None = None
        # Resting patient-limit entry awaiting a fill; chased at market after
        # entry_chase_polls polls, canceled at the flatten cutoff.
        self._pending_limit: dict[str, Any] | None = None
        # Live quote captured by the pre-entry spread check; reused for the
        # patient-limit price so we don't refetch.
        self._entry_quote: Quote | None = None
        # Order bookkeeping for outcome logging: entry/exit orders we submitted,
        # fills already logged, and entry fill prices for P&L math.
        self._entry_order_ids: set[int] = set()
        self._exit_order_ids: set[int] = set()
        self._logged_fills: set[int] = set()
        self._entry_fills: dict[str, Decimal] = {}
        # Consecutive confirmation passes per symbol/direction for double
        # confirmation; entries need two in a row when the config requires it.
        self._confirm_streak: dict[tuple[str, str], int] = {}
        self._confirm_streak_date: Any | None = None

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
        self._chase_pending_limit_entry(summary)
        self._log_fills(summary)
        positions = self.broker.get_positions()
        if self._pending_entry is not None:
            pending = positions.get(self._pending_entry)
            if pending is not None and pending.quantity != 0:
                self._pending_entry = None  # fill is now visible; managed normally
        for symbol, position in positions.items():
            if position.quantity != 0:
                summary.positions_seen[symbol] = position
                self._maybe_exit(symbol, position, summary)

        if self.flatten_before_close:
            self._maybe_flatten(positions, summary)

        if self.allow_entries and not self._has_open_position(positions) and self._pending_entry is None:
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
        return any(position.quantity != 0 for position in positions.values())

    def _maybe_exit(self, symbol: str, position: Position, summary: LoopSummary) -> None:
        plan = self.exit_plans.get(symbol)
        if plan is None:
            return
        quote = self.broker.get_latest_quote(symbol)
        if quote is None:
            return
        self._record_quotes([quote])
        if not self._can_trade_now():
            return
        session = self._exit_trading_session()
        if self._exit_blocked.get(symbol) == session:
            return  # already rejected this session; retry when the session changes

        try:
            if plan.direction == "short":
                self._maybe_exit_short(symbol, position, plan, quote, session, summary)
                return
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
                self._exit_order_ids.add(order.order_id)
                self._exit_blocked.pop(symbol, None)
            elif quote.bid <= plan.stop_price:
                # The stop has already been breached — exit immediately with a
                # market order (a resting stop below the market is invalid).
                # Night session accepts LIMIT only, so use an aggressive limit
                # at the current bid outside regular hours.
                if self._market_open():
                    order = self.broker.submit_order(
                        symbol, OrderSide.SELL, position.quantity, OrderType.MARKET,
                        trading_session="CORE",
                    )
                else:
                    order = self.broker.submit_order(
                        symbol, OrderSide.SELL, position.quantity, OrderType.LIMIT,
                        limit_price=float(quote.bid),
                        trading_session="NIGHT",
                    )
                summary.exits.append(order)
                summary.events.append(
                    LoopEvent("stop_loss_order", symbol, f"submitted {position.quantity} at {quote.bid}")
                )
                self._exit_order_ids.add(order.order_id)
                self._exit_blocked.pop(symbol, None)
        except Exception as exc:
            if self._is_deterministic_rejection(exc):
                self._exit_blocked[symbol] = session
                summary.events.append(
                    LoopEvent("exit_blocked", symbol, f"{session} rejected exit ({_error_summary(exc)}); will retry next session")
                )
            else:
                raise

    def _maybe_exit_short(
        self,
        symbol: str,
        position: Position,
        plan: ExitPlan,
        quote: Quote,
        session: str,
        summary: LoopSummary,
    ) -> None:
        """Short exit: buy to cover at the ask — target below entry, stop above."""
        if not self._market_open():
            return  # shorts can only be covered during regular hours
        quantity = abs(position.quantity)
        if quantity < 1:
            return
        if quote.ask <= plan.target_price:
            order = self.broker.submit_order(
                symbol, OrderSide.BUY, quantity, OrderType.LIMIT,
                limit_price=float(plan.target_price),
                trading_session=session,
            )
            summary.exits.append(order)
            summary.events.append(
                LoopEvent("take_profit_order", symbol, f"covered {quantity} at {quote.ask}")
            )
            self._exit_order_ids.add(order.order_id)
            self._exit_blocked.pop(symbol, None)
        elif quote.ask >= plan.stop_price:
            # The stop has already been breached — cover immediately with a
            # market buy (a resting stop above the market is invalid). Night
            # session accepts LIMIT only, so use an aggressive limit at the ask.
            if self._market_open():
                order = self.broker.submit_order(
                    symbol, OrderSide.BUY, quantity, OrderType.MARKET,
                    trading_session="CORE",
                )
            else:
                order = self.broker.submit_order(
                    symbol, OrderSide.BUY, quantity, OrderType.LIMIT,
                    limit_price=float(quote.ask),
                    trading_session="NIGHT",
                )
            summary.exits.append(order)
            summary.events.append(
                LoopEvent("stop_loss_order", symbol, f"covered {quantity} at {quote.ask}")
            )
            self._exit_order_ids.add(order.order_id)
            self._exit_blocked.pop(symbol, None)

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
            if position.quantity == 0 or symbol in self._flattened:
                continue
            # Longs sell to flatten; shorts buy to cover.
            side = OrderSide.SELL if position.quantity > 0 else OrderSide.BUY
            order = self.broker.submit_order(
                symbol, side, abs(position.quantity), OrderType.MARKET,
            )
            summary.exits.append(order)
            summary.events.append(
                LoopEvent("eod_flatten", symbol, f"market-sold {position.quantity} before close")
            )
            self._exit_order_ids.add(order.order_id)
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
        if not self._market_open() or self._in_entry_cooldown():
            return
        today = self._now().date()
        if self._entry_blocked_date != today:
            self._entry_blocked.clear()
            self._entry_blocked_date = today
        if self._confirm_streak_date != today:
            self._confirm_streak.clear()
            self._confirm_streak_date = today
        snapshots = [s for s in self._gather_snapshots() if s.symbol not in self._entry_blocked]
        if not snapshots:
            return
        self._record(snapshots)

        chosen = self._select_candidate(snapshots, summary)
        if chosen is None:
            return
        self._enter(chosen, summary)

    def _record(self, snapshots: list[MarketSnapshot]) -> None:
        """Persist this cycle's decision data for later study (best effort)."""
        if self.recorder is None:
            return
        quotes = [
            Quote(s.symbol, Decimal(str(s.bid)), Decimal(str(s.ask)), Decimal(str(s.price)))
            for s in snapshots
        ]
        try:
            self.recorder.record_step(self._now(), snapshots, quotes)
        except Exception:
            pass  # recording must never break a trading cycle

    def _record_quotes(self, quotes: list[Quote]) -> None:
        """Persist live quotes for held positions (best effort)."""
        if self.recorder is None:
            return
        try:
            self.recorder.record_step(self._now(), [], quotes)
        except Exception:
            pass

    def _gather_snapshots(self) -> list[MarketSnapshot]:
        """Combine momentum (gainers), pre-pop (most active), and loser discovery."""
        if self.snapshot_provider is not None:
            return self.snapshot_provider()
        if not isinstance(self.broker, WebullSandboxBroker) or self.broker.session is None:
            return []
        from .autoscreen import auto_screen_snapshots

        snapshots = auto_screen_snapshots(self.broker, self.config, source="gainers")
        prepop = auto_screen_snapshots(self.broker, self.config, source="most_active")
        # Losers join the hunt when shorting is enabled.
        losers = (
            auto_screen_snapshots(self.broker, self.config, source="losers")
            if self.allow_shorts
            else []
        )
        # Deduplicate by symbol, keeping the first occurrence.
        seen: dict[str, MarketSnapshot] = {}
        for snapshot in snapshots + prepop + losers:
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
            symbol = profiled.candidate.snapshot.symbol
            if self._passes_confirmation(
                symbol,
                summary,
                direction=self._profile_side(profiled.profile),
                fast=profiled.profile == "prepop",
            ):
                return profiled, False

        # Forced fallback: relax gates progressively and take the best-odds survivor.
        if not self.config.allow_fallback:
            return None
        return self._best_odds_fallback(snapshots, summary)

    @staticmethod
    def _profile_side(profile: str) -> str:
        """Bearish profiles (fade/loser) confirm against the mirrored bear stack."""
        return "bearish" if profile in ("fade", "loser") else "bullish"

    def _ranked_profiles(self, snapshots: list[MarketSnapshot]) -> list[ProfiledCandidate]:
        """Rank momentum, pre-pop, and (when enabled) fade and loser candidates."""
        combined = [
            ProfiledCandidate(candidate, "momentum")
            for candidate in screen_candidates(snapshots, self.config)
        ] + [
            ProfiledCandidate(candidate, "prepop")
            for candidate in screen_prepop_candidates(snapshots, self.config)
        ]
        if self.allow_shorts:
            combined += [
                ProfiledCandidate(candidate, "fade")
                for candidate in screen_fade_candidates(snapshots, self.config)
            ]
            combined += [
                ProfiledCandidate(candidate, "loser")
                for candidate in screen_loser_candidates(snapshots, self.config)
            ]
        # Pre-pop leads: a coiling stock is worth more than an already-exploded
        # one. Within each profile, the best score wins.
        priority = {"prepop": 0, "momentum": 1, "fade": 2, "loser": 3, "fallback": 9}
        combined.sort(key=lambda item: (priority.get(item.profile, 9), -item.candidate.score))
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

    def _entry_limit_price(self, quote: Quote, direction: str) -> float:
        """Limit a fraction of the spread inside the bid (longs) or ask (shorts)."""
        spread = quote.ask - quote.bid
        fraction = Decimal(str(self.config.entry_limit_spread_fraction))
        if direction == "short":
            return float(quote.ask - (spread * fraction))
        return float(quote.bid + (spread * fraction))

    def _local_order(self, order_id: Any) -> Any | None:
        """Find a tracked order locally (PaperBroker.orders / Webull adapter._orders)."""
        orders = getattr(self.broker, "orders", None)
        if orders is None:
            orders = getattr(self.broker, "_orders", None)
        if isinstance(orders, dict):
            return orders.get(order_id)
        return None

    def _tracked_orders(self) -> list[Any]:
        """All locally tracked orders (PaperBroker.orders / Webull adapter._orders)."""
        orders = getattr(self.broker, "orders", None)
        if orders is None:
            orders = getattr(self.broker, "_orders", None)
        if isinstance(orders, dict):
            return list(orders.values())
        return []

    def _log_fills(self, summary: LoopSummary) -> None:
        """Log newly filled entries/exits with prices; exits include dollar P&L.

        Entry fills also re-anchor the exit bracket to the ACTUAL fill price —
        with patient-limit entries the fill can sit a few percent below the
        decision price, and the bracket should guard what we truly paid.
        """
        for order in self._tracked_orders():
            if (
                order.order_id in self._logged_fills
                or order.status is not OrderStatus.FILLED
                or order.fill_price is None
            ):
                continue
            if order.order_id in self._entry_order_ids:
                self._on_entry_filled(order, summary)
            elif order.order_id in self._exit_order_ids:
                self._on_exit_filled(order, summary)
            else:
                continue  # not one of ours (e.g. externally submitted)
            self._logged_fills.add(order.order_id)

    def _on_entry_filled(self, order: Any, summary: LoopSummary) -> None:
        """Record the entry fill and re-anchor the bracket to the true price."""
        fill = order.fill_price
        self._entry_fills[order.symbol] = fill
        plan = self.exit_plans.get(order.symbol)
        if plan is not None:
            stop_pct = Decimal(str(self.config.stop_pct))
            target_pct = Decimal(str(self.config.target_pct))
            if plan.direction == "short":
                target = fill * (Decimal("1") - target_pct)
                stop = fill * (Decimal("1") + stop_pct)
            else:
                target = fill * (Decimal("1") + target_pct)
                stop = fill * (Decimal("1") - stop_pct)
            self.exit_plans[order.symbol] = ExitPlan(
                symbol=order.symbol,
                quantity=plan.quantity,
                target_price=target,
                stop_price=stop,
                direction=plan.direction,
            )
            self._persist_plans()
        summary.events.append(
            LoopEvent("entry_filled", order.symbol, f"filled {order.quantity} @ {fill}; bracket anchored to fill")
        )

    def _on_exit_filled(self, order: Any, summary: LoopSummary) -> None:
        """Log the exit fill with the trade's realized dollar P&L."""
        fill = order.fill_price
        verb = "covered" if order.side is OrderSide.BUY else "sold"
        entry_price = self._entry_fills.pop(order.symbol, None)
        if entry_price is None:
            detail = f"{verb} {order.quantity} @ {fill} (entry price unknown)"
        else:
            if order.side is OrderSide.SELL:
                pnl = (fill - entry_price) * order.quantity
            else:  # buy-to-cover a short
                pnl = (entry_price - fill) * order.quantity
            sign = "+" if pnl >= 0 else "-"
            outcome = "win" if pnl > 0 else ("loss" if pnl < 0 else "flat")
            detail = f"{verb} {order.quantity} @ {fill} (entry {entry_price}) P&L {sign}${abs(pnl):,.2f} [{outcome}]"
        summary.events.append(LoopEvent("exit_filled", order.symbol, detail))

    def _chase_pending_limit_entry(self, summary: LoopSummary) -> None:
        """Resolve a resting patient-limit entry each cycle.

        The limit gets entry_chase_polls polls to fill; after that we take the
        entry at market rather than miss the trade. Past the flatten cutoff we
        cancel instead — no new fills into the close.
        """
        pending = self._pending_limit
        if pending is None:
            return
        order = self._local_order(pending["order_id"])
        if order is not None and order.status is not OrderStatus.OPEN:
            self._pending_limit = None
            if order.status is OrderStatus.CANCELED:
                self._pending_entry = None
                self.exit_plans.pop(pending["symbol"], None)
            return  # FILLED: position management takes over
        symbol = pending["symbol"]
        if self._at_flatten_time():
            self.broker.cancel_order(pending["order_id"])
            self._pending_limit = None
            self._pending_entry = None
            self.exit_plans.pop(symbol, None)
            summary.events.append(
                LoopEvent("entry_canceled", symbol, "limit entry unfilled at the flatten cutoff; canceled")
            )
            return
        pending["polls"] += 1
        if pending["polls"] < self.config.entry_chase_polls:
            return
        try:
            self.broker.cancel_order(pending["order_id"])
            chased = self.broker.submit_order(
                symbol, pending["side"], pending["quantity"], OrderType.MARKET,
            )
        except Exception as exc:
            if self._is_deterministic_rejection(exc):
                self._entry_blocked.add(symbol)
                self._pending_limit = None
                self._pending_entry = None
                self.exit_plans.pop(symbol, None)
                summary.events.append(
                    LoopEvent("entry_blocked", symbol, "chase rejected by platform; skipped for today")
                )
                return
            raise
        summary.entries.append(chased)
        self._entry_order_ids.add(chased.order_id)
        summary.events.append(
            LoopEvent("entry_chase", symbol, "patient limit unfilled; chased at market")
        )
        self._pending_limit = None

    def _enter(
        self,
        chosen: tuple[ProfiledCandidate, bool],
        summary: LoopSummary,
    ) -> None:
        profiled, is_fallback = chosen
        # With the grain only: bullish profiles buy, bearish profiles (fade/loser)
        # short. No contrarian paths.
        if profiled.profile in ("fade", "loser"):
            if not self.allow_shorts:
                return  # shorting not enabled
            if not self._market_open():
                return  # Webull only accepts shorts during regular hours
            self._enter_short(profiled, summary)
            return
        snapshot = profiled.candidate.snapshot
        if not is_fallback and not self._entry_gates_pass(snapshot.symbol, "long", summary):
            return
        if not self._spread_ok_at_entry(snapshot.symbol, summary):
            return
        # Fixed-percentage bracket: works at the open before a day's range exists.
        entry = Decimal(str(snapshot.price))
        stop_price = entry * (Decimal("1") - Decimal(str(self.config.stop_pct)))
        try:
            plan = build_trade_plan(
                snapshot.price, float(stop_price), self.config, account_value=self._account_value()
            )
        except ValueError:
            return
        # Patient entry: rest a limit just inside the spread instead of paying
        # the full ask; if it doesn't fill within entry_chase_polls, chase it
        # at market rather than miss the trade.
        quote = self._entry_quote
        use_limit = (
            self.config.entry_chase_polls > 0
            and quote is not None
            and quote.symbol == snapshot.symbol
            and quote.bid > 0
            and quote.ask > quote.bid
        )
        try:
            if use_limit:
                order = self.broker.submit_order(
                    snapshot.symbol, OrderSide.BUY, plan.quantity, OrderType.LIMIT,
                    limit_price=self._entry_limit_price(quote, "long"),
                )
            else:
                order = self.broker.submit_order(
                    snapshot.symbol, OrderSide.BUY, plan.quantity, OrderType.MARKET,
                )
        except Exception as exc:
            if self._is_deterministic_rejection(exc):
                # Platform rejected the entry (buying-power headroom, quantity
                # cap, eligibility). Block this candidate for today and move on.
                self._entry_blocked.add(snapshot.symbol)
                summary.events.append(
                    LoopEvent("entry_blocked", snapshot.symbol, "entry rejected by platform; skipped for today")
                )
                return
            raise
        summary.entries.append(order)
        self._pending_entry = snapshot.symbol
        self._entry_order_ids.add(order.order_id)
        if use_limit:
            self._pending_limit = {
                "order_id": order.order_id,
                "symbol": snapshot.symbol,
                "side": OrderSide.BUY,
                "quantity": plan.quantity,
                "polls": 0,
            }
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
            target_price=entry * (Decimal("1") + Decimal(str(self.config.target_pct))),
            stop_price=plan.stop_price,
        )
        if is_fallback:
            summary.events.append(
                LoopEvent("fallback_entry", snapshot.symbol, "entered on relaxed best-odds gates")
            )

    def _enter_short(self, profiled: ProfiledCandidate, summary: LoopSummary) -> None:
        """Enter a short on a fade/loser candidate: stop above, target below."""
        snapshot = profiled.candidate.snapshot
        if not self._entry_gates_pass(snapshot.symbol, "short", summary):
            return
        if not self._spread_ok_at_entry(snapshot.symbol, summary):
            return
        # Fixed-percentage bracket: stop 10% above entry, target 30% below.
        entry = Decimal(str(snapshot.price))
        stop_price = entry * (Decimal("1") + Decimal(str(self.config.stop_pct)))
        try:
            plan = build_short_plan(
                snapshot.price, float(stop_price), self.config, account_value=self._account_value()
            )
        except ValueError:
            return
        # Patient short entry: rest a limit just under the ask instead of
        # selling straight into the bid; chased at market if unfilled.
        quote = self._entry_quote
        use_limit = (
            self.config.entry_chase_polls > 0
            and quote is not None
            and quote.symbol == snapshot.symbol
            and quote.bid > 0
            and quote.ask > quote.bid
        )
        try:
            if use_limit:
                order = self.broker.submit_order(
                    snapshot.symbol, OrderSide.SHORT, plan.quantity, OrderType.LIMIT,
                    limit_price=self._entry_limit_price(quote, "short"),
                )
            else:
                order = self.broker.submit_order(
                    snapshot.symbol, OrderSide.SHORT, plan.quantity, OrderType.MARKET,
                )
        except Exception as exc:
            if self._is_deterministic_rejection(exc):
                self._entry_blocked.add(snapshot.symbol)
                summary.events.append(
                    LoopEvent("entry_blocked", snapshot.symbol, f"short entry rejected: {_error_summary(exc)}")
                )
                return
            raise
        summary.entries.append(order)
        self._pending_entry = snapshot.symbol
        self._entry_order_ids.add(order.order_id)
        if use_limit:
            self._pending_limit = {
                "order_id": order.order_id,
                "symbol": snapshot.symbol,
                "side": OrderSide.SHORT,
                "quantity": plan.quantity,
                "polls": 0,
            }
        summary.events.append(
            LoopEvent(
                "entry",
                snapshot.symbol,
                f"shorted {plan.quantity} [{profiled.profile}] (score {profiled.candidate.score})",
            )
        )
        self.exit_plans[snapshot.symbol] = ExitPlan(
            symbol=snapshot.symbol,
            quantity=plan.quantity,
            target_price=entry * (Decimal("1") - Decimal(str(self.config.target_pct))),
            stop_price=plan.stop_price,
            direction="short",
        )

    def _spread_ok_at_entry(self, symbol: str, summary: LoopSummary) -> bool:
        """Re-check the live spread right before entry; quotes may have widened."""
        self._entry_quote = None
        quote = self.broker.get_latest_quote(symbol)
        if quote is None or quote.last <= 0:
            return True  # no fresh quote — don't block on missing information
        self._entry_quote = quote
        spread_pct = ((quote.ask - quote.bid) / quote.last) * 100
        if spread_pct > self.config.max_spread_percent:
            summary.events.append(
                LoopEvent("spread_rejected", symbol, f"live spread {spread_pct:.2f}% too wide at entry")
            )
            return False
        return True

    def _entry_gates_pass(self, symbol: str, direction: str, summary: LoopSummary) -> bool:
        """Accuracy gates for confirmed entries: tape alignment with the direction."""
        if not self.config.require_trend_alignment:
            return True
        if not isinstance(self.broker, WebullSandboxBroker) or self.broker.session is None:
            return True
        from .patterns import trend_direction

        trend = trend_direction(self.broker, symbol)
        if trend is None:
            return True  # no bar data — don't block on missing information
        aligned = (direction == "long" and trend == "up") or (direction == "short" and trend == "down")
        if not aligned:
            summary.events.append(
                LoopEvent("trend_misaligned", symbol, f"{direction} entry skipped: tape is {trend}")
            )
        return aligned

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

    def _passes_confirmation(
        self,
        symbol: str,
        summary: LoopSummary,
        direction: str = "bullish",
        fast: bool = False,
    ) -> bool:
        if self.confirmation is None:
            return True
        report = self._confirm(symbol, direction)
        score = getattr(report, "score", None)
        passed = getattr(report, "passed", None)
        if passed is None and score is not None:
            passed = score >= self.min_confirmation_score
        if passed is None:
            return True
        # Quorum: enough signal groups must have real data behind the score.
        signals_present = getattr(report, "signals_present", None)
        if passed and signals_present is not None and signals_present < self.config.min_signals_present:
            passed = False
            summary.events.append(
                LoopEvent("insufficient_signals", symbol, f"only {signals_present} signal group(s) with data")
            )
        # Double confirmation: two consecutive passes before entry. Pre-pop
        # candidates are early by definition — one pass is enough when enabled.
        key = (symbol, direction)
        require_double = self.config.require_double_confirmation and not (
            fast and self.config.prepop_fast_trigger
        )
        if passed and require_double:
            streak = self._confirm_streak.get(key, 0) + 1
            self._confirm_streak[key] = streak
            if streak < 2:
                summary.events.append(
                    LoopEvent("confirmation_pending", symbol, f"{direction} pass 1/2; needs a second consecutive pass")
                )
                passed = False
        elif not passed:
            self._confirm_streak.pop(key, None)
        summary.events.append(
            LoopEvent(
                "confirmation",
                symbol,
                f"{direction} score {score if score is not None else 'n/a'} -> {'pass' if passed else 'reject'}",
            )
        )
        return bool(passed)

    def _confirm(self, symbol: str, direction: str = "bullish") -> Any:
        """Run confirmation, supplying the shared market backdrop when available."""
        if (self.confirmation in (confirm_candidate, None)) and isinstance(self.broker, WebullSandboxBroker):
            confirm = confirm_candidate if direction == "bullish" else confirm_candidate_bearish
            return confirm(
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

    def _in_entry_cooldown(self) -> bool:
        """True during the first entry_cooldown_minutes after the open."""
        if self.config.entry_cooldown_minutes <= 0:
            return False
        now = self._now()
        session = get_trading_session(now.date())
        if session is None:
            return False
        from datetime import timedelta

        return session.market_open <= now < session.market_open + timedelta(
            minutes=self.config.entry_cooldown_minutes
        )


def _default_now() -> "datetime":
    from datetime import datetime

    from .schedule import EASTERN

    return datetime.now(EASTERN)


def _error_summary(exc: Exception) -> str:
    """Compact one-line description of a platform rejection for log events."""
    text = str(exc)
    for marker in ("Code: ", "Msg: "):
        if marker in text:
            text = text.split(marker, 1)[1]
    return text.split(", RequestID")[0].strip()[:120]


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
