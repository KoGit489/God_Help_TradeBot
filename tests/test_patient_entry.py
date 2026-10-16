"""Patient limit entries, the chase, and the prepop fast trigger."""

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker, Quote
from god_help_tradebot.paper import OrderType
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.screen import MarketSnapshot


def _open_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _snap(symbol: str = "GOW", price: float = 3.0, change: float = 5.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol, price, change, 500_000, 100_000,
        price - 0.01, price + 0.01, price * 0.9, price * 1.1,
    )


def _prepop_snap(symbol: str = "QUIET") -> MarketSnapshot:
    # Quiet coil: +1.5% change, 9x relative volume, mid-range — pure pre-pop.
    return MarketSnapshot(symbol, 2.50, 1.5, 900_000, 100_000, 2.49, 2.51, 2.45, 2.55)


def _broker_with_quote(symbol: str = "GOW", bid: str = "2.99", ask: str = "3.01") -> PaperBroker:
    broker = PaperBroker(10_000)
    broker.last_quotes[symbol] = Quote(symbol, Decimal(bid), Decimal(ask), Decimal(bid))
    return broker


def _always_pass(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(score=0.9, passed=True, signals_present=3)


# --- Patient limit entries ---------------------------------------------------


def test_entry_rests_a_patient_limit_inside_the_spread() -> None:
    broker = _broker_with_quote()
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snap()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    entry = summary.entries[0]
    assert entry.order_type is OrderType.LIMIT
    # Bid 2.99 + 25% of the 2-cent spread = 2.995 — we no longer pay the ask.
    assert entry.limit_price == Decimal("2.995")
    assert entry.status.value == "OPEN"  # resting patiently, not chasing


def test_unfilled_limit_is_chased_at_market() -> None:
    broker = _broker_with_quote()
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snap()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()  # submits the patient limit
    summary = loop.run_once(summary)  # waits (poll 1 of 2)
    assert not any(e.kind == "entry_chase" for e in summary.events)

    summary = loop.run_once(summary)  # patience over — chase at market

    assert any(e.kind == "entry_chase" for e in summary.events)
    assert summary.entries[-1].order_type is OrderType.MARKET


def test_pending_limit_is_canceled_at_the_flatten_cutoff() -> None:
    broker = _broker_with_quote()
    state = {"now": _open_now()}
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snap()],
        now_provider=lambda: state["now"],
        allow_entries=True,
        flatten_before_close=True,
    )

    summary = loop.run_once()  # patient limit resting
    state["now"] = datetime(2026, 10, 7, 15, 59, 30, tzinfo=EASTERN)
    loop.run_once(summary)

    assert any(e.kind == "entry_canceled" for e in summary.events)
    assert loop._pending_limit is None
    assert loop._pending_entry is None
    assert "GOW" not in loop.exit_plans


# --- Prepop fast trigger -------------------------------------------------------


def test_prepop_enters_on_a_single_confirmation() -> None:
    broker = _broker_with_quote("QUIET", "2.49", "2.51")
    loop = MonitorLoop(
        broker,
        BotConfig(require_double_confirmation=True),
        confirmation=_always_pass,
        snapshot_provider=lambda: [_prepop_snap()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries and summary.entries[0].symbol == "QUIET"
    assert not any(e.kind == "confirmation_pending" for e in summary.events)


def test_momentum_still_requires_double_confirmation() -> None:
    broker = _broker_with_quote()
    loop = MonitorLoop(
        broker,
        BotConfig(require_double_confirmation=True, allow_fallback=False),
        confirmation=_always_pass,
        snapshot_provider=lambda: [_snap()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    first = loop.run_once()
    assert not first.entries
    assert any(e.kind == "confirmation_pending" for e in first.events)

    second = loop.run_once(first)
    assert second.entries and second.entries[0].symbol == "GOW"


# --- Prepop leads the ranking ---------------------------------------------------


def test_prepop_beats_momentum_in_selection_order() -> None:
    broker = PaperBroker(10_000)
    broker.last_quotes["QUIET"] = Quote("QUIET", Decimal("2.49"), Decimal("2.51"), Decimal("2.50"))
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snap("FAST", 3.0, 15.0), _prepop_snap()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].symbol == "QUIET"
    assert "[prepop]" in summary.events[0].detail
