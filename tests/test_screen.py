from god_help_tradebot import BotConfig, MarketSnapshot, screen_candidates
from god_help_tradebot.screen import explosive_score, screen_loser_candidates


def snapshot(symbol: str, change: float, relative_volume: float, spread: float) -> MarketSnapshot:
    price = 2.00
    return MarketSnapshot(
        symbol=symbol,
        price=price,
        change_percent=change,
        volume=int(100_000 * relative_volume),
        average_volume=100_000,
        bid=price - (price * spread / 200),
        ask=price + (price * spread / 200),
        day_low=1.50,
        day_high=2.125,
    )


def test_screen_filters_and_ranks_candidates() -> None:
    ranked = screen_candidates(
        [
            snapshot("LOWVOL", 15, 1.5, 0.2),
            snapshot("LEADER", 12, 6, 0.2),
            snapshot("WIDE", 25, 8, 2.0),
        ],
        BotConfig(),
    )

    assert [candidate.snapshot.symbol for candidate in ranked] == ["LEADER"]
    assert ranked[0].score > 0


def test_screen_rejects_overextended_move() -> None:
    ranked = screen_candidates([snapshot("EXTENDED", 41, 10, 0.2)], BotConfig())

    assert ranked == []


def test_snapshot_metrics_are_transparent() -> None:
    candidate = snapshot("TEST", 10, 4, 0.5)

    assert candidate.relative_volume == 4
    assert candidate.spread_percent == 0.5
    assert candidate.range_position == 0.8


# --- Explosive-potential ranking (same requirements, better leaderboard) ------


def _explosive_snap(symbol: str, change: float, rvol: float, range_position: float, price: float = 3.00) -> MarketSnapshot:
    """Snapshot with controlled rvol/range position and a tight 0.4% spread."""
    low = price - 0.10
    high = low + (0.10 / range_position) if range_position > 0 else low
    half_spread = price * 0.002
    return MarketSnapshot(
        symbol=symbol,
        price=price,
        change_percent=change,
        volume=int(100_000 * rvol),
        average_volume=100_000,
        bid=price - half_spread,
        ask=price + half_spread,
        day_low=low,
        day_high=high,
    )


def test_explosive_ranking_prefers_volume_surge_pressing_the_high() -> None:
    slow = _explosive_snap("SLOW", change=8, rvol=3, range_position=0.5)
    fast = _explosive_snap("FAST", change=15, rvol=12, range_position=0.9)

    ranked = screen_candidates([slow, fast], BotConfig())

    assert ranked[0].snapshot.symbol == "FAST"
    assert ranked[0].score > ranked[1].score


def test_explosive_score_is_direction_aware() -> None:
    pressing_high = _explosive_snap("UP", change=10, rvol=8, range_position=0.9)
    pinned_low = _explosive_snap("DOWN", change=10, rvol=8, range_position=0.1)

    assert explosive_score(pressing_high, "long") > explosive_score(pinned_low, "long")
    assert explosive_score(pinned_low, "short") > explosive_score(pressing_high, "short")


def test_loser_profile_ranks_most_explosive_slide_first() -> None:
    drifting = _explosive_snap("MEH", change=-5, rvol=2.5, range_position=0.35)
    collapsing = _explosive_snap("DROP", change=-18, rvol=9, range_position=0.05)

    ranked = screen_loser_candidates([drifting, collapsing], BotConfig())

    assert ranked[0].snapshot.symbol == "DROP"


def test_explosive_but_disqualified_still_gets_filtered() -> None:
    # Same pricing requirements: a $15 stock is over max_symbol_price no matter
    # how explosive it looks.
    too_expensive = _explosive_snap("RICH", change=30, rvol=20, range_position=0.95, price=15.00)

    assert screen_candidates([too_expensive], BotConfig()) == []