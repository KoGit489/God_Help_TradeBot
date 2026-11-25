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
from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .config import BotConfig
from .monitor import LoopSummary, MonitorLoop
from .paper import OrderSide, OrderStatus, OrderType, PaperOrder, PaperBroker, Position, Quote
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
        open_price=float(data["open_price"]) if data.get("open_price") is not None else None,
        previous_close=float(data["previous_close"]) if data.get("previous_close") is not None else None,
        overnight_price=float(data["overnight_price"]) if data.get("overnight_price") is not None else None,
        overnight_change_ratio=float(data["overnight_change_ratio"]) if data.get("overnight_change_ratio") is not None else None,
        overnight_volume=float(data["overnight_volume"]) if data.get("overnight_volume") is not None else None,
        extended_price=float(data["extended_price"]) if data.get("extended_price") is not None else None,
        extended_change_ratio=float(data["extended_change_ratio"]) if data.get("extended_change_ratio") is not None else None,
        extended_volume=float(data["extended_volume"]) if data.get("extended_volume") is not None else None,
    )


def _quote_from(data: dict[str, Any]) -> Quote:
    return Quote(
        str(data["symbol"]).upper(),
        Decimal(str(data["bid"])),
        Decimal(str(data["ask"])),
        Decimal(str(data["last"])),
        Decimal(str(data["bid_volume"])) if data.get("bid_volume") is not None else None,
        Decimal(str(data["ask_volume"])) if data.get("ask_volume") is not None else None,
    )


def load_timeline(path: str | Path) -> list[dict[str, Any]]:
    """Load a backtest timeline from JSON ({"steps": [...]}) or recorded JSONL."""
    text = Path(path).read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
        if isinstance(payload, list):
            steps = payload
        elif isinstance(payload, dict) and "time" in payload:
            steps = [payload]
        else:
            steps = payload.get("steps", []) if isinstance(payload, dict) else []
    except json.JSONDecodeError:
        # Recorded day files are JSONL: one step object per line.
        steps = [json.loads(line) for line in text.splitlines() if line.strip()]
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


@dataclass(frozen=True)
class ExecutionStress:
    spread_multiplier: float = 1.0
    slippage_bps: float = 2.0
    fee_per_share: float = 0.005
    partial_fraction: float = 1.0
    reject_every: int = 0
    outage_every: int = 0
    disable_borrow: bool = False
    borrow_daily_bps: float = 1.0


class ResearchBroker(PaperBroker):
    def __init__(self, starting_cash: float, stress: ExecutionStress) -> None:
        super().__init__(starting_cash)
        self.stress = stress
        self.now: datetime | None = None
        self.shortable: set[str] = set()
        self.halted: set[str] = set()
        self.sessions: dict[int, str] = {}
        self.quote_times: dict[str, datetime] = {}
        self.costs = Decimal("0")
        self.order_attempts = 0
        self.fill_events = 0
        self.outage = False

    def get_buying_power(self) -> Decimal:
        short_commitment = sum((abs(position.quantity) * position.average_price * 2
                                for position in self.positions.values() if position.quantity < 0), Decimal("0"))
        return max(Decimal("0"), self.cash - short_commitment)

    def get_latest_quote(self, symbol: str) -> Quote | None:
        when = self.quote_times.get(symbol)
        if self.outage or when is None or self.now is None or (self.now - when).total_seconds() > 30:
            return None
        return super().get_latest_quote(symbol)

    def submit_order(self, symbol: str, side: OrderSide, quantity: int, order_type: OrderType,
                     *, trading_session: str = "CORE", **kwargs: Any) -> PaperOrder:
        from .schedule import equity_order_session

        self.order_attempts += 1
        if self.now is None or equity_order_session(self.now) == "CLOSED" or quantity > 199999:
            raise RuntimeError("OPENAPI_SIM_REJECTION: session or quantity unavailable")
        if side is OrderSide.SHORT and (self.stress.disable_borrow or symbol not in self.shortable or trading_session != "CORE"):
            raise RuntimeError("OPENAPI_SIM_REJECTION: historical borrow unavailable")
        if trading_session != "CORE" and order_type is not OrderType.LIMIT:
            raise RuntimeError("OPENAPI_SIM_REJECTION: outside-hours market order")
        if self.stress.reject_every and self.order_attempts % self.stress.reject_every == 0:
            raise RuntimeError("OPENAPI_SIM_REJECTION: modeled rejection")
        order = super().submit_order(symbol, side, quantity, order_type, trading_session=trading_session, **kwargs)
        order.client_order_id = f"simulation-{order.order_id}"
        self.sessions[order.order_id] = trading_session
        return order

    def process_quote(self, quote: Quote) -> list[PaperOrder]:
        from .schedule import equity_order_session

        if self.outage:
            return []
        self.last_quotes[quote.symbol] = quote
        self.quote_times[quote.symbol] = self.now
        if quote.symbol in self.halted or equity_order_session(self.now) == "CLOSED":
            return []
        filled = []
        for order in list(self.orders.values()):
            if order.status is not OrderStatus.OPEN or order.symbol != quote.symbol:
                continue
            session = equity_order_session(self.now)
            supported = self.sessions[order.order_id]
            if (supported == "NIGHT" and session != "NIGHT") or (supported == "CORE" and session != "CORE") or (supported == "ALL" and session == "NIGHT"):
                order.status = OrderStatus.CANCELED
                continue
            price = self._fill_price(order, quote)
            if price is None:
                continue
            sign = 1 if order.side is OrderSide.BUY else -1
            price *= 1 + Decimal(str(self.stress.slippage_bps)) / 10000 * sign
            if order.limit_price is not None and ((order.side is OrderSide.BUY and price > order.limit_price) or (order.side is not OrderSide.BUY and price < order.limit_price)):
                continue
            remaining = order.quantity - order.filled_quantity
            depth = quote.ask_volume if order.side is OrderSide.BUY else quote.bid_volume
            if depth is None or depth <= 0:
                continue
            quantity = min(remaining, max(1, int(remaining * self.stress.partial_fraction)), int(depth))
            if quantity < 1:
                continue
            clone = replace(order, quantity=quantity, filled_quantity=0, status=OrderStatus.OPEN)
            self._apply_fill(clone, price)
            notional = (order.fill_price or Decimal("0")) * order.filled_quantity + price * quantity
            order.filled_quantity += quantity
            order.fill_price = notional / order.filled_quantity
            order.status = OrderStatus.FILLED if order.filled_quantity == order.quantity else OrderStatus.OPEN
            fee = Decimal(str(self.stress.fee_per_share)) * quantity
            self.cash -= fee
            self.costs += fee
            self.fill_events += 1
            filled.append(order)
        return filled


def run_research_backtest(timeline: list[dict[str, Any]], config: BotConfig, directory: Path,
                          stress: ExecutionStress | None = None, *, starting_cash: float = 100000) -> dict[str, Any]:
    from types import SimpleNamespace
    from .state import EquityPerformanceStore

    directory.mkdir(parents=True, exist_ok=True)
    ledger = EquityPerformanceStore(directory / "execution.json", "isolated-research")
    if ledger.data["trades"] or ledger.data["active"] or ledger.data["orders"]:
        raise ValueError("research replay requires a fresh isolated directory")
    stress = stress or ExecutionStress()
    broker = ResearchBroker(starting_cash, stress)
    ordered = []
    previous = None
    for step in timeline:
        when = datetime.fromisoformat(str(step["time"]))
        if when.tzinfo is None or (previous is not None and when <= previous):
            raise ValueError("research timeline must be timezone-aware and strictly chronological")
        ordered.append((when, step))
        previous = when
    if not ordered:
        return {"trades": 0, "net_pnl": 0.0, "drawdown": 0.0, "loss_severity": 0.0,
                "open_exposure": False, "errors": 0, "missing_signals": 0, "fill_events": 0}
    broker.now = ordered[0][0]
    current_signals: dict[str, Any] = {}
    missing = 0

    def confirmation(symbol: str, direction: str = "bullish") -> Any:
        nonlocal missing
        record = current_signals.get(symbol, {}).get(direction)
        if not isinstance(record, dict):
            missing += 1
            return SimpleNamespace(passed=False, score=0.0, signals_present=0)
        return SimpleNamespace(**record)

    loop = MonitorLoop(broker, config, now_provider=lambda: broker.now, allow_entries=True,
                       performance_store=ledger, confirmation=lambda symbol: confirmation(symbol))
    loop._confirm = confirmation
    summary = LoopSummary()
    peak_equity = Decimal(str(starting_cash))
    drawdown = Decimal("0")
    intent_times = {}
    seen_events = 0
    previous_day = ordered[0][0].date()
    income = Decimal("0")
    for index, (when, step) in enumerate(ordered):
        broker.now = when
        elapsed_days = (when.date() - previous_day).days
        if elapsed_days > 0:
            borrowing = sum((abs(position.quantity) * position.average_price for position in broker.positions.values() if position.quantity < 0), Decimal("0"))
            cost = borrowing * Decimal(str(stress.borrow_daily_bps)) / 10000 * elapsed_days
            broker.cash -= cost
            broker.costs += cost
            previous_day = when.date()
        current_signals = step.get("confirmations", {})
        broker.shortable = {symbol for symbol, allowed in step.get("shortable", {}).items() if allowed is True}
        broker.halted = set(step.get("halted", []))
        broker.outage = bool(stress.outage_every and (index + 1) % stress.outage_every == 0)
        for action in step.get("corporate_actions", []):
            position = broker.positions.get(action["symbol"])
            if position is None or not position.quantity:
                continue
            if action["type"] == "dividend":
                amount = position.quantity * Decimal(str(action["amount"]))
                broker.cash += amount
                income += amount
            elif action["type"] == "split":
                ratio = Decimal(str(action["ratio"]))
                quantity = Decimal(position.quantity) * ratio
                if ratio <= 0 or quantity != quantity.to_integral_value() or any(order.status is OrderStatus.OPEN for order in broker.orders.values()):
                    raise ValueError("split with fractional shares or pending orders requires verified corporate-action handling")
                position.quantity = int(quantity)
                position.average_price /= ratio
                trade = ledger.data["active"].get(action["symbol"])
                if trade is not None:
                    for name in ("entry_quantity", "exit_quantity"):
                        adjusted = Decimal(trade[name]) * ratio
                        if adjusted != adjusted.to_integral_value():
                            raise ValueError("fractional split accounting unavailable")
                        trade[name] = int(adjusted)
                    ledger.save()
                    loop._entry_fills[action["symbol"]] = position.average_price
                plan = loop.exit_plans.get(action["symbol"])
                if plan is not None:
                    loop.exit_plans[action["symbol"]] = replace(plan, quantity=abs(position.quantity), target_price=plan.target_price / ratio,
                        stop_price=plan.stop_price / ratio, favorable_extreme=plan.favorable_extreme / ratio if plan.favorable_extreme else None,
                        last_sample_price=None, last_sample_time=None, reversal_samples=0)
        quotes = []
        for raw in step.get("quotes", []):
            quote = _quote_from(raw)
            midpoint = (quote.bid + quote.ask) / 2
            width = (quote.ask - quote.bid) * Decimal(str(stress.spread_multiplier)) / 2
            quote = replace(quote, bid=max(Decimal("0.0001"), midpoint - width), ask=midpoint + width)
            quotes.append(quote)
            broker.process_quote(quote)
        quote_map = {quote.symbol: quote for quote in quotes}
        snapshots = [replace(snapshot, bid=float(quote_map[snapshot.symbol].bid), ask=float(quote_map[snapshot.symbol].ask))
                     for snapshot in (_snapshot_from(raw) for raw in step.get("snapshots", [])) if snapshot.symbol in quote_map]
        loop.snapshot_provider = lambda: snapshots
        loop._peak_confirmation_cache.clear()
        loop.run_once(summary)
        for event in summary.events[seen_events:]:
            if event.kind == "entry":
                intent_times[event.symbol] = when.isoformat()
        seen_events = len(summary.events)
        for symbol, trade in ledger.data["active"].items():
            if "research_intent" not in trade:
                trade["research_intent"] = intent_times.get(symbol)
        ledger.save()
        equity = broker.cash + sum((position.quantity * broker.last_quotes[symbol].last
                                   for symbol, position in broker.positions.items() if symbol in broker.last_quotes), Decimal("0"))
        peak_equity = max(peak_equity, equity)
        if peak_equity > 0:
            drawdown = max(drawdown, (peak_equity - equity) / peak_equity)
    gross = sum((Decimal(trade["realized_pnl_usd"]) for trade in ledger.data["trades"]), Decimal("0"))
    losses = [Decimal(trade["realized_pnl_usd"]) / Decimal(trade["entry_notional"]) for trade in ledger.data["trades"]]
    closed_equity = 1.0
    closed_peak = 1.0
    closed_drawdown = 0.0
    for trade in ledger.data["trades"]:
        net = float(Decimal(trade["realized_pnl_usd"]) - Decimal(str(stress.fee_per_share)) * (trade["entry_quantity"] + trade["exit_quantity"]))
        closed_equity *= 1 + net / float(trade["entry_notional"])
        closed_peak = max(closed_peak, closed_equity)
        closed_drawdown = max(closed_drawdown, (closed_peak - closed_equity) / closed_peak)
    return {"trades": len(ledger.data["trades"]), "net_pnl": float(gross - broker.costs + income), "drawdown": float(drawdown),
            "closed_drawdown": closed_drawdown,
            "loss_severity": float(abs(min(losses, default=Decimal("0")))),
            "win_rate": sum(trade["outcome"] == "win" for trade in ledger.data["trades"]) / len(losses) if losses else 0.0,
            "open_exposure": bool(ledger.data["active"] or ledger.data["orders"]),
            "errors": sum(event.kind == "error" for event in summary.events), "missing_signals": missing,
            "fill_events": broker.fill_events, "fees": float(broker.costs), "gross_pnl": float(gross),
            "trade_keys": [f"{trade['symbol']}:{trade['direction']}:{trade['research_intent']}" for trade in ledger.data["trades"] if trade.get("research_intent")]}
