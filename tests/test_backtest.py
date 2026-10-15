from god_help_tradebot import BotConfig
from god_help_tradebot.backtest import load_timeline, run_backtest


def _snap(symbol, price, day_low, day_high):
    return {
        "symbol": symbol, "price": price, "change_percent": 5.0,
        "volume": 500_000, "average_volume": 100_000,
        "bid": price - 0.01, "ask": price + 0.01,
        "day_low": day_low, "day_high": day_high,
    }


def _quote(symbol, bid, ask, last):
    return {"symbol": symbol, "bid": bid, "ask": ask, "last": last}


def _timeline_winning_long() -> list[dict]:
    # Entry at 10:00 near 3.01 ask; target 30% above = 3.90; price rallies past it.
    return [
        {"time": "2026-10-07T10:00:00-04:00",
         "snapshots": [_snap("GOW", 3.00, 2.80, 3.20)],
         "quotes": [_quote("GOW", 2.99, 3.01, 3.00)]},
        {"time": "2026-10-07T10:05:00-04:00",
         "snapshots": [_snap("GOW", 3.50, 2.80, 3.60)],
         "quotes": [_quote("GOW", 3.49, 3.51, 3.50)]},
        {"time": "2026-10-07T10:10:00-04:00",
         "snapshots": [_snap("GOW", 3.95, 2.80, 4.00)],
         "quotes": [_quote("GOW", 3.95, 3.96, 3.95)]},
        # Final step fills the exit order placed in the previous step.
        {"time": "2026-10-07T10:15:00-04:00",
         "snapshots": [_snap("GOW", 3.95, 2.80, 4.00)],
         "quotes": [_quote("GOW", 3.95, 3.96, 3.95)]},
    ]


def _timeline_losing_long() -> list[dict]:
    # Entry near 3.01; stop 10% below = 2.70; price slides through it.
    return [
        {"time": "2026-10-07T10:00:00-04:00",
         "snapshots": [_snap("GOW", 3.00, 2.80, 3.20)],
         "quotes": [_quote("GOW", 2.99, 3.01, 3.00)]},
        {"time": "2026-10-07T10:05:00-04:00",
         "snapshots": [_snap("GOW", 2.75, 2.70, 3.20)],
         "quotes": [_quote("GOW", 2.75, 2.76, 2.75)]},
        {"time": "2026-10-07T10:10:00-04:00",
         "snapshots": [_snap("GOW", 2.69, 2.60, 3.20)],
         "quotes": [_quote("GOW", 2.68, 2.69, 2.68)]},
        # Final step fills the exit order placed in the previous step.
        {"time": "2026-10-07T10:15:00-04:00",
         "snapshots": [_snap("GOW", 2.69, 2.60, 3.20)],
         "quotes": [_quote("GOW", 2.68, 2.69, 2.68)]},
    ]


def test_backtest_reports_a_winning_long_trade() -> None:
    result = run_backtest(_timeline_winning_long(), BotConfig(allow_fallback=False))

    assert result.total_trades == 1
    assert result.wins == 1
    assert result.losses == 0
    assert result.win_rate == 1.0
    assert result.realized_pnl > 0
    assert result.trades[0].side == "long"


def test_backtest_reports_a_losing_long_trade() -> None:
    result = run_backtest(_timeline_losing_long(), BotConfig(allow_fallback=False))

    assert result.total_trades == 1
    assert result.losses == 1
    assert result.realized_pnl < 0


def test_backtest_no_trades_when_nothing_qualifies(tmp_path) -> None:
    empty = tmp_path / "t.json"
    empty.write_text('{"steps": []}', encoding="utf-8")

    result = run_backtest(load_timeline(empty), BotConfig(allow_fallback=False))

    assert result.total_trades == 0
    assert result.win_rate == 0.0
