from datetime import datetime
from decimal import Decimal

from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker
from god_help_tradebot.screen import (
    MarketSnapshot,
    best_candidate,
    screen_candidates,
    screen_prepop_candidates,
)
from god_help_tradebot.schedule import EASTERN


def _open_market_now() -> datetime:
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


def test_prepop_screen_finds_quiet_accumulation() -> None:
    # High relative volume, tiny move, coiling near the low.
    snapshot = _snapshot("QUIET", 2.50, change_percent=1.5, relative_volume=4.0, day_low=2.45, day_high=2.60)
    config = BotConfig()

    ranked = screen_prepop_candidates([snapshot], config)

    assert len(ranked) == 1
    assert ranked[0].snapshot.symbol == "QUIET"
    # Momentum screen would reject this (change < min 2%), so it's pre-pop only.
    assert screen_candidates([snapshot], config) == []


def test_prepop_screen_rejects_already_popped() -> None:
    popped = _snapshot("POPPED", 3.00, change_percent=25.0, relative_volume=6.0, day_low=2.40, day_high=3.20)
    config = BotConfig()

    assert screen_prepop_candidates([popped], config) == []


def test_prepop_screen_rejects_low_relative_volume() -> None:
    quiet = _snapshot("SLOW", 2.50, change_percent=1.0, relative_volume=1.2, day_low=2.45, day_high=2.60)
    assert screen_prepop_candidates([quiet], BotConfig()) == []


def test_best_candidate_prefers_higher_score_across_profiles() -> None:
    momentum = _snapshot("POPPED", 3.00, change_percent=25.0, relative_volume=8.0, day_low=2.40, day_high=3.20)
    prepop = _snapshot("QUIET", 2.50, change_percent=1.5, relative_volume=9.0, day_low=2.45, day_high=2.60)
    config = BotConfig()

    chosen = best_candidate([momentum, prepop], config)

    assert chosen is not None
    assert chosen.profile in ("momentum", "prepop")
    assert chosen.candidate.score > 0


def test_best_candidate_returns_none_when_nothing_qualifies() -> None:
    junk = _snapshot("JUNK", 50.0, change_percent=500.0, relative_volume=0.5, day_low=40.0, day_high=60.0)
    assert best_candidate([junk], BotConfig()) is None


def test_monitor_takes_prepop_when_it_outscores_momentum() -> None:
    broker = PaperBroker(10_000)
    prepop = _snapshot("QUIET", 2.50, change_percent=1.5, relative_volume=9.0, day_low=2.45, day_high=2.55)
    weak_momentum = _snapshot("WEAK", 3.00, change_percent=2.1, relative_volume=2.1, day_low=2.90, day_high=3.00)
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [prepop, weak_momentum],
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries[0].symbol == "QUIET"
    assert "[prepop]" in summary.events[0].detail


def test_monitor_forces_fallback_entry_when_nothing_passes() -> None:
    broker = PaperBroker(10_000)
    # Fails normal momentum AND prepop gates, but passes relaxed fallback gates.
    fallback = _snapshot("ODDS", 4.00, change_percent=6.0, relative_volume=1.5, day_low=3.90, day_high=4.20)
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [fallback],
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries[0].symbol == "ODDS"
    assert any(e.kind == "fallback_entry" for e in summary.events)
    assert any(e.kind == "entry" and "[fallback]" in e.detail for e in summary.events)


def test_monitor_no_entry_when_fallback_also_fails() -> None:
    broker = PaperBroker(10_000)
    junk = _snapshot("JUNK", 50.0, change_percent=6.0, relative_volume=1.5, day_low=49.0, day_high=52.0)
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [junk],
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries == []


def test_monitor_fallback_skips_confirmation() -> None:
    broker = PaperBroker(10_000)
    fallback = _snapshot("ODDS", 4.00, change_percent=6.0, relative_volume=1.5, day_low=3.90, day_high=4.20)
    calls = []

    def _confirmation(symbol: str):
        calls.append(symbol)
        raise AssertionError("confirmation must be skipped for fallback entries")

    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [fallback],
        confirmation=_confirmation,
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries[0].symbol == "ODDS"
    assert calls == []
