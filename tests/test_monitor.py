from datetime import datetime, time
from decimal import Decimal

from god_help_tradebot import (
    BotConfig,
    ExitPlan,
    MonitorLoop,
    OrderStatus,
    OrderType,
    OrderSide,
    PaperBroker,
    Position,
    Quote,
)
from god_help_tradebot.monitor import LoopSummary
from god_help_tradebot.screen import MarketSnapshot
from god_help_tradebot.schedule import EASTERN


def _open_market_now() -> datetime:
    # A mid-session Wednesday keeps get_trading_session inside market hours.
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _closed_market_now() -> datetime:
    return datetime(2026, 10, 7, 18, 0, tzinfo=EASTERN)


def _snapshot(symbol: str, price: float, day_low: float, day_high: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        price=price,
        change_percent=5.0,
        volume=500_000,
        average_volume=100_000,
        bid=price - 0.01,
        ask=price + 0.01,
        day_low=day_low,
        day_high=day_high,
    )


def _broker_with_position(symbol: str, quantity: int, avg: float, quote: Quote) -> PaperBroker:
    broker = PaperBroker(10_000)
    broker.positions[symbol] = Position(quantity=quantity, average_price=Decimal(str(avg)))
    broker.last_quotes[symbol] = quote
    return broker


def test_monitor_exits_at_target_price() -> None:
    quote = Quote("NIVF", Decimal("0.15"), Decimal("0.16"), Decimal("0.15"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    plan = ExitPlan(symbol="NIVF", quantity=100, target_price=Decimal("0.15"), stop_price=Decimal("0.09"))
    loop = MonitorLoop(broker, exit_plans={"NIVF": plan}, now_provider=_closed_market_now)

    summary = loop.run_once()

    assert summary.exits[0].order_type is OrderType.LIMIT
    assert summary.exits[0].status is OrderStatus.FILLED
    assert summary.events[0].kind == "take_profit"


def test_monitor_exits_at_stop_price() -> None:
    quote = Quote("NIVF", Decimal("0.08"), Decimal("0.09"), Decimal("0.08"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    plan = ExitPlan(symbol="NIVF", quantity=100, target_price=Decimal("0.15"), stop_price=Decimal("0.09"))
    loop = MonitorLoop(broker, exit_plans={"NIVF": plan}, now_provider=_closed_market_now)

    summary = loop.run_once()

    assert summary.exits[0].order_type is OrderType.STOP
    assert summary.events[0].kind == "stop_loss"


def test_monitor_holds_within_bracket() -> None:
    quote = Quote("NIVF", Decimal("0.11"), Decimal("0.12"), Decimal("0.11"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    plan = ExitPlan(symbol="NIVF", quantity=100, target_price=Decimal("0.15"), stop_price=Decimal("0.09"))
    loop = MonitorLoop(broker, exit_plans={"NIVF": plan}, now_provider=_closed_market_now)

    summary = loop.run_once()

    assert summary.exits == []
    assert summary.events == []


def test_monitor_skips_position_without_exit_plan() -> None:
    quote = Quote("NIVF", Decimal("0.08"), Decimal("0.09"), Decimal("0.08"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    loop = MonitorLoop(broker, now_provider=_closed_market_now)

    summary = loop.run_once()

    assert summary.exits == []
    assert summary.positions_seen["NIVF"].quantity == 100


def test_monitor_enters_top_candidate_when_market_open() -> None:
    broker = PaperBroker(10_000)
    config = BotConfig(max_symbol_price=10.0, risk_per_trade_usd=25.0, max_position_value_usd=500.0)
    loop = MonitorLoop(
        broker,
        config,
        snapshot_provider=lambda: [_snapshot("GOW", 3.00, 2.80, 3.20)],
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert len(summary.entries) == 1
    assert summary.events[0].kind == "entry"
    assert "GOW" in loop.exit_plans
    assert summary.entries[0].side is OrderSide.BUY


def test_monitor_does_not_enter_when_market_closed() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snapshot("GOW", 3.00, 2.80, 3.20)],
        now_provider=_closed_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries == []
    assert loop.exit_plans == {}


def test_monitor_does_not_enter_while_holding() -> None:
    quote = Quote("NIVF", Decimal("0.11"), Decimal("0.12"), Decimal("0.11"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    plan = ExitPlan(symbol="NIVF", quantity=100, target_price=Decimal("0.15"), stop_price=Decimal("0.09"))
    loop = MonitorLoop(
        broker,
        exit_plans={"NIVF": plan},
        snapshot_provider=lambda: [_snapshot("GOW", 3.00, 2.80, 3.20)],
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries == []


def test_monitor_run_respects_max_polls() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(broker, now_provider=_closed_market_now, poll_seconds=0, max_polls=3)

    summary = loop.run()

    assert summary.polls == 3


def test_monitor_run_once_returns_cumulative_summary() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(broker, now_provider=_closed_market_now)
    summary = LoopSummary()

    loop.run_once(summary)
    loop.run_once(summary)

    assert summary.polls == 2
