"""Trade outcome logging: fill prices, P&L, and fill-anchored brackets."""

from datetime import datetime
from decimal import Decimal

from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker, Quote
from god_help_tradebot.monitor import ExitPlan
from god_help_tradebot.paper import OrderSide, OrderStatus, OrderType, PaperOrder, Position
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.screen import MarketSnapshot


def _open_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _snap(symbol: str = "GOW", price: float = 3.0, change: float = 5.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol, price, change, 500_000, 100_000,
        price - 0.01, price + 0.01, price * 0.9, price * 1.1,
    )


def _quote(symbol: str, bid: str, ask: str) -> Quote:
    return Quote(symbol, Decimal(bid), Decimal(ask), Decimal(bid))


def _entered_loop() -> tuple[MonitorLoop, PaperBroker]:
    """Loop with a resting patient-limit entry at 2.995 on GOW."""
    broker = PaperBroker(10_000)
    broker.last_quotes["GOW"] = _quote("GOW", "2.99", "3.01")
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snap()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )
    loop.run_once()
    return loop, broker


def test_entry_fill_logs_price_and_reanchors_bracket() -> None:
    loop, broker = _entered_loop()
    plan = loop.exit_plans["GOW"]
    assert plan.target_price == Decimal("3.9")  # anchored to the 3.00 decision price

    # The market comes to us: ask drops to 2.95, filling the resting limit.
    broker.process_quote(_quote("GOW", "2.94", "2.95"))
    summary = loop.run_once()

    assert any(e.kind == "entry_filled" and "@ 2.95" in e.detail for e in summary.events)
    # Bracket re-anchored to the true 2.95 fill, not the 3.00 decision price.
    plan = loop.exit_plans["GOW"]
    assert plan.target_price == Decimal("2.95") * Decimal("1.3")
    assert plan.stop_price == Decimal("2.95") * Decimal("0.9")


def test_exit_fill_logs_winning_pnl() -> None:
    loop, broker = _entered_loop()
    broker.process_quote(_quote("GOW", "2.94", "2.95"))  # fills entry at 2.95
    loop.run_once()

    broker.process_quote(_quote("GOW", "3.84", "3.85"))  # bid over the 3.835 target
    loop.run_once()  # take-profit submitted
    broker.process_quote(_quote("GOW", "3.84", "3.85"))  # fills the exit
    summary = loop.run_once()

    filled = [e for e in summary.events if e.kind == "exit_filled"]
    assert filled, "expected an exit_filled event"
    assert "P&L +$" in filled[0].detail
    assert "[win]" in filled[0].detail
    assert "(entry 2.95)" in filled[0].detail


def test_exit_fill_logs_losing_pnl() -> None:
    loop, broker = _entered_loop()
    broker.process_quote(_quote("GOW", "2.94", "2.95"))  # fills entry at 2.95
    loop.run_once()

    broker.process_quote(_quote("GOW", "2.64", "2.65"))  # bid under the 2.655 stop
    loop.run_once()  # breached stop exits at market
    broker.process_quote(_quote("GOW", "2.64", "2.65"))  # fills the exit
    summary = loop.run_once()

    filled = [e for e in summary.events if e.kind == "exit_filled"]
    assert filled, "expected an exit_filled event"
    assert "P&L -$" in filled[0].detail
    assert "[loss]" in filled[0].detail


def test_exit_fill_without_known_entry_still_logs() -> None:
    # Position and plan predate a restart: no recorded entry fill, still logs.
    broker = PaperBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    broker.last_quotes["GOW"] = _quote("GOW", "3.26", "3.27")
    loop = MonitorLoop(
        broker,
        BotConfig(allow_fallback=False),
        exit_plans={"GOW": ExitPlan("GOW", 100, Decimal("3.25"), Decimal("2.25"))},
        now_provider=_open_now,
        flatten_before_close=False,
    )

    loop.run_once()  # take-profit submitted (bid 3.26 >= 3.25 target)
    broker.process_quote(_quote("GOW", "3.26", "3.27"))  # fills at the bid
    summary = loop.run_once()

    filled = [e for e in summary.events if e.kind == "exit_filled"]
    assert filled, "expected an exit_filled event"
    assert "entry price unknown" in filled[0].detail
    assert "@ 3.26" in filled[0].detail


def test_partial_exit_fills_keep_entry_reference_for_pnl() -> None:
    loop = MonitorLoop(PaperBroker(10_000), now_provider=_open_now, flatten_before_close=False)
    loop._entry_fills["GOW"] = Decimal("2.50")
    from god_help_tradebot.monitor import LoopSummary

    result = LoopSummary()
    for order_id, fill_price in ((1, "2.40"), (2, "2.35")):
        order = PaperOrder(
            order_id=order_id,
            symbol="GOW",
            side=OrderSide.SELL,
            quantity=50,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            fill_price=Decimal(fill_price),
        )
        loop._on_exit_filled(order, result)

    exit_events = [event for event in result.events if event.kind == "exit_filled"]
    assert len(exit_events) == 2
    assert all("(entry 2.50)" in event.detail for event in exit_events)
    assert "P&L -$5.00" in exit_events[0].detail
    assert "P&L -$7.50" in exit_events[1].detail
