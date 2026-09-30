from god_help_tradebot import BotConfig, MarketSnapshot, screen_candidates


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