"""Exit watchdog: fill verification, aggressive chase, ghost cleanup, startup alarm."""

from datetime import datetime
from decimal import Decimal

from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker, Quote, StateStore
from god_help_tradebot.monitor import ExitPlan
from god_help_tradebot.paper import OrderStatus, OrderType, Position
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.broker import WebullSandboxBroker
from god_help_tradebot.monitor import LoopSummary
from god_help_tradebot.paper import OrderSide, PaperOrder


def test_webull_watchdog_does_not_churn_exit_without_bid_depth() -> None:
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    order = PaperOrder(1, "RMSG", OrderSide.SELL, 1000, OrderType.MARKET)
    broker._orders = {1: order}
    broker.get_latest_quote = lambda symbol: Quote(symbol, Decimal("0.24"), Decimal("0.25"), Decimal("0.25"), Decimal("0"), Decimal("100"))
    broker.cancel_order = lambda order_id: (_ for _ in ()).throw(AssertionError("must not cancel without liquidity"))
    loop = MonitorLoop(broker, now_provider=_open_now)
    loop._track_exit("RMSG", order, OrderSide.SELL, 1000)
    result = LoopSummary()
    positions = {"RMSG": Position(1000, Decimal("0.28"))}
    for _ in range(5):
        loop._watchdog_pending_exits(result, positions)
    assert len([event for event in result.events if event.kind == "exit_liquidity_wait"]) == 1
    assert not result.exits


def test_unconfirmed_webull_cancel_blocks_replacement() -> None:
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    order = PaperOrder(1, "RMSG", OrderSide.SELL, 1000, OrderType.MARKET, client_order_id="test")
    broker._orders = {1: order}
    broker.cancel_order = lambda order_id: setattr(order, "status", OrderStatus.CANCELED)
    broker.get_order_status = lambda client_order_id: {"status": "SUBMITTED"}
    loop = MonitorLoop(broker, now_provider=_open_now)
    result = LoopSummary()
    assert not loop._cancel_confirmed(1, "RMSG", result)
    assert order.status is OrderStatus.OPEN
    assert result.events[-1].kind == "cancel_pending"
    broker.get_order_status = lambda client_order_id: {"status": "CANCELLED"}
    assert loop._cancel_confirmed(1, "RMSG", result)


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


def test_359_flatten_uses_core_session_order() -> None:
    broker = PaperBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    broker.last_quotes["GOW"] = _quote("GOW", "2.40", "2.42")
    cutoff = datetime(2026, 10, 7, 15, 59, 30, tzinfo=EASTERN)
    loop = MonitorLoop(
        broker,
        exit_plans={"GOW": ExitPlan("GOW", 100, Decimal("3.25"), Decimal("2.25"))},
        now_provider=lambda: cutoff,
        flatten_before_close=True,
    )

    summary = loop.run_once()

    assert summary.exits[0].order_type is OrderType.MARKET
    assert any(e.kind == "eod_flatten" and "market-exit" in e.detail for e in summary.events)


def test_flatten_waits_during_the_4_to_8_pm_dead_zone() -> None:
    broker = PaperBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    broker.last_quotes["GOW"] = _quote("GOW", "2.40", "2.42")
    closed = datetime(2026, 10, 7, 16, 30, tzinfo=EASTERN)
    loop = MonitorLoop(
        broker,
        exit_plans={"GOW": ExitPlan("GOW", 100, Decimal("3.25"), Decimal("2.25"))},
        now_provider=lambda: closed,
        flatten_before_close=True,
    )

    summary = loop.run_once()

    assert summary.exits == []
    assert not any(e.kind == "error" for e in summary.events)


def test_flatten_halves_quantity_after_webull_share_limit_rejection() -> None:
    class _ShareLimitedBroker(PaperBroker):
        def submit_order(self, symbol, side, quantity, order_type, **kwargs):
            if quantity > 50_000:
                raise RuntimeError(
                    "HTTP Status: 417, Code: OPENAPI_ORDER_RISK_RULE_SHARES, "
                    "maximum quantity value allowed per order"
                )
            return super().submit_order(symbol, side, quantity, order_type, **kwargs)

    broker = _ShareLimitedBroker(10_000)
    broker.positions["GOW"] = Position(quantity=199_999, average_price=Decimal("0.25"))
    broker.last_quotes["GOW"] = _quote("GOW", "0.24", "0.25")
    cutoff = datetime(2026, 10, 7, 15, 59, 30, tzinfo=EASTERN)
    loop = MonitorLoop(
        broker,
        now_provider=lambda: cutoff,
        flatten_before_close=True,
    )

    summary = loop.run_once()

    assert summary.exits[0].quantity == 49_999
    assert any(e.kind == "exit_quantity_reduced" for e in summary.events)
    assert not any(e.kind == "error" for e in summary.events)
    assert "GOW" in loop._flattened  # no duplicate while the partial exit is working

    broker.process_quote(_quote("GOW", "0.23", "0.24"))
    next_summary = loop.run_once(summary)

    assert broker.positions["GOW"].quantity == 150_000
    assert len(next_summary.exits) == 2  # remaining position gets another managed exit
    assert next_summary.exits[-1].quantity == 37_500


def test_deterministic_flatten_rejection_does_not_halt_loop() -> None:
    class _RejectedFlattenBroker(PaperBroker):
        def submit_order(self, symbol, side, quantity, order_type, **kwargs):
            if side.value == "SELL":
                raise RuntimeError(
                    "HTTP Status: 417, Code: OPENAPI_OVERNIGHT_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS"
                )
            return super().submit_order(symbol, side, quantity, order_type, **kwargs)

    broker = _RejectedFlattenBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    cutoff = datetime(2026, 10, 7, 15, 59, 30, tzinfo=EASTERN)
    loop = MonitorLoop(
        broker,
        now_provider=lambda: cutoff,
        flatten_before_close=True,
    )

    first = loop.run_once()
    second = loop.run_once(first)

    assert any(e.kind == "exit_blocked" for e in first.events)
    assert not any(e.kind == "error" for e in second.events)
    assert "GOW" in loop._exit_blocked


def test_canceled_core_flatten_is_retried_during_night_session() -> None:
    broker = PaperBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    broker.last_quotes["GOW"] = _quote("GOW", "2.40", "2.42")
    state = {"now": datetime(2026, 10, 7, 15, 59, 30, tzinfo=EASTERN)}
    loop = MonitorLoop(
        broker,
        exit_plans={"GOW": ExitPlan("GOW", 100, Decimal("3.25"), Decimal("2.25"))},
        now_provider=lambda: state["now"],
        flatten_before_close=True,
    )

    loop.run_once()  # CORE market flatten submitted
    order = broker.orders[1]
    order.status = OrderStatus.CANCELED  # exchange canceled it at the close
    state["now"] = datetime(2026, 10, 7, 16, 30, tzinfo=EASTERN)
    loop.run_once()  # clear the terminal order and flattened marker

    assert "GOW" not in loop._flattened
    state["now"] = datetime(2026, 10, 7, 20, 30, tzinfo=EASTERN)
    summary = loop.run_once()

    assert summary.exits[-1].order_type is OrderType.LIMIT
    assert any(e.kind == "eod_flatten" and "night session" in e.detail for e in summary.events)


def test_flatten_plan_persists_until_position_is_confirmed_flat(tmp_path) -> None:
    broker = PaperBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    broker.last_quotes["GOW"] = _quote("GOW", "2.40", "2.42")
    store = StateStore(tmp_path / "bot_state.json")
    cutoff = datetime(2026, 10, 7, 15, 59, 30, tzinfo=EASTERN)
    loop = MonitorLoop(
        broker,
        exit_plans={"GOW": ExitPlan("GOW", 100, Decimal("3.25"), Decimal("2.25"))},
        state_store=store,
        now_provider=lambda: cutoff,
        flatten_before_close=True,
    )

    loop.run_once()  # order submitted but not yet filled
    assert "GOW" in store.load()

    broker.process_quote(_quote("GOW", "2.39", "2.40"))
    loop.run_once()  # fill reconciled, position now flat

    assert store.load() == {}
