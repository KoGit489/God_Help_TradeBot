"""Paradox fallback: low-conviction fallback reads trade the OPPOSITE direction."""

from datetime import datetime
from types import SimpleNamespace

from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker
from god_help_tradebot.broker import WebullSandboxBroker
from god_help_tradebot.paper import OrderSide
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.screen import MarketSnapshot


def _open_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _snapshot(
    symbol: str,
    price: float,
    change_percent: float,
    relative_volume: float,
    day_low: float,
    day_high: float,
) -> MarketSnapshot:
    avg_volume = 100_000
    return MarketSnapshot(
        symbol=symbol,
        price=price,
        change_percent=change_percent,
        volume=int(avg_volume * relative_volume),
        average_volume=avg_volume,
        bid=price - 0.01,
        ask=price + 0.01,
        day_low=day_low,
        day_high=day_high,
    )


# Bullish fallback: +6% but only 1.5x rvol — fails the real gates (rvol >= 2),
# survives the relaxed fallback screen.
def _bullish_fallback() -> MarketSnapshot:
    return _snapshot("ODDS", 4.00, change_percent=6.0, relative_volume=1.5, day_low=3.90, day_high=4.20)


# Bearish fallback: -6% but only 1.5x rvol, pinned near the low — fails the real
# loser gates (rvol >= 2), survives the relaxed bearish fallback screen.
def _bearish_fallback() -> MarketSnapshot:
    return _snapshot("BUMR", 4.00, change_percent=-6.0, relative_volume=1.5, day_low=3.90, day_high=4.30)


def test_bullish_fallback_gets_shorted() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=True),
        snapshot_provider=lambda: [_bullish_fallback()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].side is OrderSide.SHORT
    assert loop.exit_plans["ODDS"].direction == "short"
    assert any(e.kind == "paradox_fallback" for e in summary.events)


def test_bullish_fallback_stands_down_when_shorts_are_off() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=False),
        snapshot_provider=lambda: [_bullish_fallback()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries == []
    assert any(e.kind == "fallback_paradox_blocked" for e in summary.events)


def test_bearish_fallback_gets_bought_long() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=True),
        snapshot_provider=lambda: [_bearish_fallback()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].side is OrderSide.BUY
    assert loop.exit_plans["BUMR"].direction == "long"
    assert any(e.kind == "paradox_fallback" and "buying it instead" in e.detail for e in summary.events)


def test_bearish_fallback_needs_shorts_enabled() -> None:
    # Bearish reads only exist when the loser screen runs (shorts enabled);
    # with shorts off a loser was never a candidate, so there's nothing to
    # paradox — stand down.
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=False),
        snapshot_provider=lambda: [_bearish_fallback()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries == []


def test_paradox_can_be_disabled_for_with_grain_fallback() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        BotConfig(paradox_fallback=False, fallback_after_minutes=0),
        snapshot_provider=lambda: [_bullish_fallback()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].side is OrderSide.BUY  # old behavior: with the read
    assert not any(e.kind == "paradox_fallback" for e in summary.events)


# --- Safety rail 1: fallbacks wait for the tape to develop ---------------------


def test_fallback_waits_for_the_first_hour() -> None:
    broker = PaperBroker(10_000)
    state = {"now": datetime(2026, 10, 7, 9, 50, tzinfo=EASTERN)}  # 20 min in
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=True),
        snapshot_provider=lambda: [_bullish_fallback()],
        now_provider=lambda: state["now"],
        allow_entries=True,
        flatten_before_close=False,
    )

    first = loop.run_once()
    assert not first.entries  # too early — nothing faded blind at the open

    state["now"] = datetime(2026, 10, 7, 10, 40, tzinfo=EASTERN)  # past the hour
    second = loop.run_once(first)
    assert second.entries and second.entries[0].side is OrderSide.SHORT


def test_fallback_window_is_tunable_to_zero() -> None:
    broker = PaperBroker(10_000)
    early = datetime(2026, 10, 7, 9, 50, tzinfo=EASTERN)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=True, fallback_after_minutes=0),
        snapshot_provider=lambda: [_bullish_fallback()],
        now_provider=lambda: early,
        allow_entries=True,
        flatten_before_close=False,
    )

    assert loop.run_once().entries  # window disabled — fallback fires at 9:50


# --- Safety rail 2: never fade a read the tape is confirming --------------------


def _webull_stub_broker(trend: str, monkeypatch) -> WebullSandboxBroker:
    """A minimally-connected Webull adapter whose tape reads `trend`."""
    import god_help_tradebot.patterns as patterns

    monkeypatch.setattr(patterns, "trend_direction", lambda broker, symbol: trend)
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    broker.session = SimpleNamespace()  # marks it connected
    broker._orders = {}
    broker.get_positions = lambda: {}
    return broker


def test_paradox_short_stands_down_on_an_up_tape(monkeypatch) -> None:
    # The bullish read isn't weak — it's RIGHT. Shorting it would be a squeeze.
    broker = _webull_stub_broker("up", monkeypatch)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=True, fallback_after_minutes=0),
        snapshot_provider=lambda: [_bullish_fallback()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries == []
    assert any(e.kind == "paradox_tape_blocked" for e in summary.events)


def test_paradox_long_stands_down_on_a_down_tape(monkeypatch) -> None:
    # The bearish read is confirmed by the tape — don't catch the falling knife.
    broker = _webull_stub_broker("down", monkeypatch)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=True, fallback_after_minutes=0),
        snapshot_provider=lambda: [_bearish_fallback()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries == []
    assert any(e.kind == "paradox_tape_blocked" for e in summary.events)


# --- Weakest conviction is the paradox's juiciest target ------------------------


def _two_bullish_survivors() -> list[MarketSnapshot]:
    # Both fail the real gates (rvol < 2) but survive the relaxed fallback.
    stronger = _snapshot("WEAK1", 4.00, change_percent=6.0, relative_volume=1.5, day_low=3.90, day_high=4.20)
    weaker = _snapshot("WEAK2", 4.00, change_percent=2.0, relative_volume=1.2, day_low=3.95, day_high=4.10)
    return [stronger, weaker]


def test_paradox_fallback_targets_the_weakest_conviction() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=True, fallback_after_minutes=0),
        snapshot_provider=_two_bullish_survivors,
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    # WEAK2 carries the lower score — the read most likely to fail — so the
    # paradox shorts IT, not the stronger survivor.
    assert summary.entries[0].symbol == "WEAK2"
    assert summary.entries[0].side is OrderSide.SHORT


def test_with_grain_fallback_keeps_the_strongest_conviction() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        BotConfig(paradox_fallback=False, fallback_after_minutes=0),
        snapshot_provider=_two_bullish_survivors,
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].symbol == "WEAK1"
    assert summary.entries[0].side is OrderSide.BUY
