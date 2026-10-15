"""Weekly self-study: replay recorded days, grade the strategy, improve it carefully.

Every study_interval_days recorded days, the bot replays its own sessions through
the backtest harness. If at least `target_winning_days` of the last window were
winning days, nothing changes. Otherwise it tries small bounded parameter nudges
and applies one ONLY if it improves the recent window AND never hurts the earlier
recorded days — improvements must carry across weeks, which is the defense
against overfitting.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from .backtest import BacktestResult, load_timeline, run_backtest
from .config import BotConfig
from .recorder import DEFAULT_DATA_DIR, list_recorded_days

DEFAULT_STUDY_STATE_FILE = Path("study_state.json")

# Bounded nudge steps: parameters only ever move one small step per study, and
# never outside these rails. Only BotConfig fields may appear here.
_NUDGE_RULES: dict[str, dict[str, Any]] = {
    "stop_pct": {"step": 0.02, "min": 0.03, "max": 0.30},
    "target_pct": {"step": 0.05, "min": 0.10, "max": 0.60},
    "entry_cooldown_minutes": {"step": 5, "min": 0, "max": 60},
}


@dataclass(frozen=True)
class DayGrade:
    day: str
    trades: int
    realized_pnl: Decimal
    winning: bool


@dataclass
class StudyReport:
    days_studied: list[DayGrade] = field(default_factory=list)
    window_winning_days: int = 0
    window_total_days: int = 0
    met_target: bool = False
    change_applied: str | None = None
    variants_tried: int = 0


def _evaluate(timeline: list[dict[str, Any]], config: BotConfig) -> BacktestResult:
    return run_backtest(
        timeline,
        config,
        confirmation=None,  # live confirmations aren't replayable offline
        allow_shorts=config.allow_shorts,
    )


def grade_days(
    day_files: list[Path],
    config: BotConfig,
) -> list[DayGrade]:
    """Replay each recorded day with the given config and grade it.

    Days with no trades are excluded — a quiet day isn't a strategy failure.
    """
    grades: list[DayGrade] = []
    for day_file in day_files:
        result = _evaluate(load_timeline(day_file), config)
        if result.total_trades == 0:
            continue
        pnl = result.realized_pnl
        grades.append(
            DayGrade(
                day=day_file.stem,
                trades=result.total_trades,
                realized_pnl=pnl,
                winning=pnl > 0,
            )
        )
    return grades


def _winning_days(grades: list[DayGrade]) -> int:
    return sum(1 for grade in grades if grade.winning)


def _candidate_variants(config: BotConfig) -> list[tuple[str, BotConfig]]:
    """Small bounded nudges: one parameter, one step, in both directions."""
    variants: list[tuple[str, BotConfig]] = []
    for field_name, rule in _NUDGE_RULES.items():
        current = getattr(config, field_name)
        for direction in (-1, 1):
            nudged = current + (rule["step"] * direction)
            nudged = max(rule["min"], min(rule["max"], nudged))
            if nudged == current:
                continue
            variants.append(
                (
                    f"{field_name} {current} -> {nudged}",
                    dataclasses.replace(config, **{field_name: nudged}),
                )
            )
    return variants


def run_study(
    config: BotConfig,
    *,
    data_dir: str | Path = DEFAULT_DATA_DIR,
    interval_days: int = 5,
    target_winning_days: int = 4,
    auto_improve: bool = True,
    config_writer: Callable[[BotConfig], None] | None = None,
) -> StudyReport:
    """Grade the recent window and, if below target, carefully improve.

    A variant is applied only when it wins MORE of the recent window than the
    current config AND wins at least as many of ALL recorded days — the
    improvement must carry across the whole history, not just last week.
    """
    report = StudyReport()
    day_files = list_recorded_days(data_dir)
    if not day_files:
        return report

    grades = grade_days(day_files, config)
    report.days_studied = grades
    window = grades[-interval_days:]
    report.window_total_days = len(window)
    report.window_winning_days = _winning_days(window)
    report.met_target = report.window_winning_days >= target_winning_days

    if report.met_target or not auto_improve:
        return report

    # Below target: try bounded variants, but only keep ones whose improvement
    # persists across the full recorded history (the anti-overfitting rule).
    base_window_wins = report.window_winning_days
    base_all_wins = _winning_days(grades)
    best: tuple[str, BotConfig, list[DayGrade]] | None = None

    for label, variant in _candidate_variants(config):
        variant_grades = grade_days(day_files, variant)
        report.variants_tried += 1
        variant_window_wins = _winning_days(variant_grades[-interval_days:])
        variant_all_wins = _winning_days(variant_grades)
        if variant_window_wins > base_window_wins and variant_all_wins >= base_all_wins:
            if best is None or variant_window_wins > _winning_days(best[2][-interval_days:]):
                best = (label, variant, variant_grades)

    if best is not None:
        label, variant, _ = best
        report.change_applied = label
        if config_writer is not None:
            config_writer(variant)

    return report


def write_config_file(config: BotConfig, path: str | Path) -> None:
    """Persist the improved config atomically to the bot_config.json file."""
    payload = {
        "stop_pct": config.stop_pct,
        "target_pct": config.target_pct,
        "min_signals_present": config.min_signals_present,
        "entry_cooldown_minutes": config.entry_cooldown_minutes,
        "position_pct_of_account": config.position_pct_of_account,
        "allow_shorts": config.allow_shorts,
        "allow_fallback": config.allow_fallback,
        "require_double_confirmation": config.require_double_confirmation,
        "require_trend_alignment": config.require_trend_alignment,
    }
    _write_json_atomic(payload, path)


def load_study_state(path: str | Path = DEFAULT_STUDY_STATE_FILE) -> dict[str, Any]:
    file = Path(path)
    if not file.exists():
        return {}
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_study_state(state: dict[str, Any], path: str | Path = DEFAULT_STUDY_STATE_FILE) -> None:
    _write_json_atomic(state, path)


def _write_json_atomic(payload: dict[str, Any], path: str | Path) -> None:
    """Write JSON via a temp file + replace so a crash can't corrupt the file."""
    target = Path(path)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(target)


__all__ = [
    "DayGrade",
    "StudyReport",
    "grade_days",
    "load_study_state",
    "run_study",
    "save_study_state",
    "write_config_file",
]
