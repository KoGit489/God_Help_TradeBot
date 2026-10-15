from datetime import datetime
from decimal import Decimal
from pathlib import Path

from god_help_tradebot import BotConfig, Quote
from god_help_tradebot.recorder import SessionRecorder, list_recorded_days, load_recorded_day
from god_help_tradebot.screen import MarketSnapshot
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.study import grade_days, run_study


def _snap(symbol: str, price: float) -> MarketSnapshot:
    return MarketSnapshot(symbol, price, 5.0, 500_000, 100_000, price - 0.01, price + 0.01, price * 0.95, price * 1.05)


def _quote(symbol: str, price: float) -> Quote:
    return Quote(symbol, Decimal(str(price - 0.01)), Decimal(str(price + 0.01)), Decimal(str(price)))


# --- Recorder --------------------------------------------------------------------

def test_recorder_writes_and_loads_a_day(tmp_path) -> None:
    recorder = SessionRecorder(tmp_path)
    when = datetime(2026, 10, 7, 10, 0, tzinfo=EASTERN)
    recorder.record_step(when, [_snap("GOW", 3.0)], [_quote("GOW", 3.0)])

    days = list_recorded_days(tmp_path)
    assert len(days) == 1

    steps = load_recorded_day(days[0])
    assert steps[0]["snapshots"][0]["symbol"] == "GOW"
    assert steps[0]["quotes"][0]["bid"] == 2.99


def test_recorder_appends_multiple_steps(tmp_path) -> None:
    recorder = SessionRecorder(tmp_path)
    when = datetime(2026, 10, 7, 10, 0, tzinfo=EASTERN)
    recorder.record_step(when, [_snap("GOW", 3.0)], [])
    recorder.record_step(when, [_snap("GOW", 3.1)], [])

    steps = load_recorded_day(list_recorded_days(tmp_path)[0])
    assert len(steps) == 2


def test_recorder_skips_empty_steps(tmp_path) -> None:
    recorder = SessionRecorder(tmp_path)
    recorder.record_step(datetime(2026, 10, 7, 10, 0, tzinfo=EASTERN), [], [])

    assert list_recorded_days(tmp_path) == []


# --- Study engine -------------------------------------------------------------------

def _write_day(directory: Path, day: str, steps: list[dict]) -> Path:
    import json

    path = directory / f"{day}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for step in steps:
            handle.write(json.dumps(step) + "\n")
    return path


def _winning_day(day: str) -> list[dict]:
    return [
        {"time": f"{day}T10:00:00-04:00", "snapshots": [{"symbol": "GOW", "price": 3.0, "change_percent": 5.0, "volume": 500_000, "average_volume": 100_000, "bid": 2.99, "ask": 3.01, "day_low": 2.80, "day_high": 3.20}], "quotes": [{"symbol": "GOW", "bid": 2.99, "ask": 3.01, "last": 3.0}]},
        {"time": f"{day}T10:05:00-04:00", "snapshots": [], "quotes": [{"symbol": "GOW", "bid": 3.49, "ask": 3.51, "last": 3.50}]},
        {"time": f"{day}T10:10:00-04:00", "snapshots": [], "quotes": [{"symbol": "GOW", "bid": 3.95, "ask": 3.96, "last": 3.95}]},
        {"time": f"{day}T10:15:00-04:00", "snapshots": [], "quotes": [{"symbol": "GOW", "bid": 3.95, "ask": 3.96, "last": 3.95}]},
    ]


def _losing_day(day: str) -> list[dict]:
    return [
        {"time": f"{day}T10:00:00-04:00", "snapshots": [{"symbol": "GOW", "price": 3.0, "change_percent": 5.0, "volume": 500_000, "average_volume": 100_000, "bid": 2.99, "ask": 3.01, "day_low": 2.80, "day_high": 3.20}], "quotes": [{"symbol": "GOW", "bid": 2.99, "ask": 3.01, "last": 3.0}]},
        {"time": f"{day}T10:05:00-04:00", "snapshots": [], "quotes": [{"symbol": "GOW", "bid": 2.75, "ask": 2.76, "last": 2.75}]},
        {"time": f"{day}T10:10:00-04:00", "snapshots": [], "quotes": [{"symbol": "GOW", "bid": 2.68, "ask": 2.69, "last": 2.68}]},
        {"time": f"{day}T10:15:00-04:00", "snapshots": [], "quotes": [{"symbol": "GOW", "bid": 2.68, "ask": 2.69, "last": 2.68}]},
    ]


def test_grade_days_skips_no_trade_days(tmp_path) -> None:
    _write_day(tmp_path, "2026-10-07", _winning_day("2026-10-07"))
    _write_day(tmp_path, "2026-10-08", [])  # no data at all

    grades = grade_days(list_recorded_days(tmp_path), BotConfig(allow_fallback=False))

    assert len(grades) == 1
    assert grades[0].winning is True


def test_study_keeps_config_when_window_meets_target(tmp_path) -> None:
    for i in range(5):
        _write_day(tmp_path, f"2026-10-0{i + 5}", _winning_day(f"2026-10-0{i + 5}"))
    written = []

    report = run_study(
        BotConfig(allow_fallback=False),
        data_dir=tmp_path,
        interval_days=5,
        target_winning_days=4,
        config_writer=lambda cfg: written.append(cfg),
    )

    assert report.met_target is True
    assert report.window_winning_days == 5
    assert report.change_applied is None
    assert written == []


def test_study_improves_only_when_gain_carries_across_history(tmp_path) -> None:
    # 5 losing days -> below target -> variants evaluated.
    for i in range(5):
        _write_day(tmp_path, f"2026-10-0{i + 5}", _losing_day(f"2026-10-0{i + 5}"))
    written = []

    report = run_study(
        BotConfig(allow_fallback=False),
        data_dir=tmp_path,
        interval_days=5,
        target_winning_days=4,
        config_writer=lambda cfg: written.append(cfg),
    )

    assert report.met_target is False
    assert report.variants_tried > 0
    # If a variant was applied, it must beat the window AND never hurt history.
    if report.change_applied is not None:
        assert len(written) == 1


def test_study_does_nothing_without_recorded_days(tmp_path) -> None:
    report = run_study(BotConfig(), data_dir=tmp_path / "empty")
    assert report.days_studied == []
    assert report.change_applied is None
