"""Paradox fallback: low-conviction fallback reads trade the OPPOSITE direction."""

from datetime import datetime

from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker
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
        BotConfig(paradox_fallback=False),
        snapshot_provider=lambda: [_bullish_fallback()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].side is OrderSide.BUY  # old behavior: with the read
    assert not any(e.kind == "paradox_fallback" for e in summary.events)
