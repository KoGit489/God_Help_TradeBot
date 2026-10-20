"""Exit watchdog: fill verification, aggressive chase, ghost cleanup, startup alarm."""

from datetime import datetime
from decimal import Decimal

from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker, Quote
from god_help_tradebot.monitor import ExitPlan
from god_help_tradebot.paper import OrderStatus, OrderType, Position
from god_help_tradebot.schedule import EASTERN


def _open_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _quote(symbol: str, bid: str, ask: str) -> Quote:
    return Quote(symbol, Decimal(bid), Decimal(ask), Decimal(bid))


def _loop_with_long_position(bid: str = "2.24", ask: str = "2.25") -> tuple[MonitorLoop, PaperBroker]:
    """Long GOW position with a stop at 2.25; the quote breaches it."""
    broker = PaperBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    broker.last_quotes["GOW"] = _quote("GOW", bid, ask)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_fallback=False),
        exit_plans={"GOW": ExitPlan("GOW", 100, Decimal("3.25"), Decimal("2.25"))},
        now_provider=_open_now,
        flatten_before_close=False,
    )
    return loop, broker


def test_pending_exit_blocks_duplicate_submits() -> None:
    loop, _ = _loop_with_long_position()

    first = loop.run_once()
    second = loop.run_once(first)  # exit already working — no resubmit

    assert len(second.exits) == 1  # without the guard, each poll stacked orders


def test_unfilled_exit_is_chased_aggressively() -> None:
    loop, _ = _loop_with_long_position()

    summary = loop.run_once()        # stop breached -> market sell submitted
    summary = loop.run_once(summary)  # watchdog waits (poll 1 of 2)
    assert not any(e.kind == "exit_chase" for e in summary.events)

    summary = loop.run_once(summary)  # patience over -> chase

    assert any(e.kind == "exit_chase" for e in summary.events)
    assert len(summary.exits) == 2
    assert summary.exits[-1].order_type is OrderType.MARKET


def test_filled_exit_clears_the_watchdog() -> None:
    loop, broker = _loop_with_long_position()

    loop.run_once()                                   # exit submitted (resting)
    broker.process_quote(_quote("GOW", "2.23", "2.24"))  # fills at the bid
    summary = loop.run_once()

    assert any(e.kind == "exit_filled" for e in summary.events)
    assert not any(e.kind == "exit_chase" for e in summary.events)
    assert "GOW" not in loop._pending_exits


def test_ghost_exit_order_is_canceled_when_position_is_flat() -> None:
    loop, broker = _loop_with_long_position()

    loop.run_once()  # exit submitted (resting)
    broker.positions["GOW"] = Position(quantity=0, average_price=Decimal("0"))  # flat by other means
    summary = loop.run_once()

    assert any(e.kind == "ghost_exit_canceled" for e in summary.events)
    assert "GOW" not in loop._pending_exits
    assert all(o.status is not OrderStatus.OPEN for o in broker.orders.values())


def test_startup_alarm_fires_once_for_carried_positions() -> None:
    # A position with NO exit plan is an orphan (e.g. a flatten that never
    # filled popped the plan) — that's what the alarm guards.
    broker = PaperBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    broker.last_quotes["GOW"] = _quote("GOW", "2.60", "2.61")
    loop = MonitorLoop(
        broker,
        BotConfig(allow_fallback=False),
        now_provider=_open_now,
        flatten_before_close=False,
    )

    first = loop.run_once()
    second = loop.run_once(first)

    alarms = [e for e in second.events if e.kind == "startup_position"]
    assert len(alarms) == 1
    assert "WARNING" in alarms[0].detail


def test_night_flatten_rests_a_limit_not_a_market() -> None:
    broker = PaperBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    broker.last_quotes["GOW"] = _quote("GOW", "2.40", "2.42")
    night = datetime(2026, 10, 7, 20, 30, tzinfo=EASTERN)  # night session, past the cutoff
    loop = MonitorLoop(
        broker,
        exit_plans={"GOW": ExitPlan("GOW", 100, Decimal("3.25"), Decimal("2.25"))},
        now_provider=lambda: night,
        flatten_before_close=True,
    )

    summary = loop.run_once()

    assert summary.exits[0].order_type is OrderType.LIMIT
    assert summary.exits[0].limit_price == Decimal("2.40")  # aggressive at the bid
    assert any(e.kind == "eod_flatten" for e in summary.events)
