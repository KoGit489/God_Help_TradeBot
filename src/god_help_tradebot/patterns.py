"""Chart-pattern detection for intraday penny-stock momentum.

Analyzes recent OHLCV bars (1m/5m/15m) for the classic day-trade setups that fit
a single-day-hold strategy: bull flags, higher-low bases, and high-of-day
breakouts with volume. Each detector returns a 0-1 score; the best pattern wins.
Missing bar data degrades to neutral rather than failing the candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .broker import WebullSandboxBroker, US_STOCK_CATEGORY


@dataclass(frozen=True)
class Bar:
    time: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class PatternReport:
    symbol: str
    score: float
    pattern: str | None
    detail: str
    has_data: bool


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def bars_from_response(payload: Any) -> list[Bar]:
    """Extract bars from the SDK's nested {result: [{result: [...]}]} shape, oldest first."""
    if isinstance(payload, dict):
        outer = payload.get("result", payload.get("data", []))
    else:
        outer = payload
    if not isinstance(outer, list):
        return []

    bars: list[Bar] = []
    for series in outer:
        if not isinstance(series, dict):
            continue
        rows = series.get("result", series.get("data", []))
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            bars.append(
                Bar(
                    time=str(row.get("time", "")),
                    open=_float(row.get("open")),
                    high=_float(row.get("high")),
                    low=_float(row.get("low")),
                    close=_float(row.get("close")),
                    volume=_float(row.get("volume")),
                )
            )
    # API returns newest first; reverse to chronological order.
    return list(reversed(bars))


def _avg(values: Iterable[float]) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def bull_flag_score(bars: list[Bar]) -> float:
    """Spike then tight consolidation on shrinking volume: the flag before continuation."""
    if len(bars) < 8:
        return 0.0
    recent = bars[-8:]
    spike = recent[:4]
    flag = recent[-4:]

    spike_move = (spike[-1].close - spike[0].open) / spike[0].open if spike[0].open else 0.0
    if spike_move < 0.02:
        return 0.0

    flag_range = max(b.high for b in flag) - min(b.low for b in flag)
    flag_mid = _avg([b.close for b in flag])
    tightness = 1 - min(flag_range / flag_mid / 0.03, 1.0) if flag_mid else 0.0

    spike_vol = _avg(b.volume for b in spike)
    flag_vol = _avg(b.volume for b in flag)
    volume_fade = min(spike_vol / flag_vol, 2.0) / 2.0 if flag_vol > 0 else 0.5

    holds_gain = 1.0 if flag[-1].close >= spike[0].open + (spike_move * spike[0].open * 0.6) else 0.3
    return round(min(spike_move / 0.10, 1.0) * 0.4 + tightness * 0.3 + volume_fade * 0.3 * holds_gain, 4)


def higher_lows_score(bars: list[Bar]) -> float:
    """Ascending base: each pullback low is higher than the last (stair-step up)."""
    if len(bars) < 6:
        return 0.0
    recent = bars[-6:]
    lows = [b.low for b in recent]
    rising = sum(1 for i in range(1, len(lows)) if lows[i] >= lows[i - 1])
    stair = rising / (len(lows) - 1)

    drift = (recent[-1].close - recent[0].open) / recent[0].open if recent[0].open else 0.0
    drift_score = min(max(drift / 0.03, 0.0), 1.0)
    return round(stair * 0.6 + drift_score * 0.4, 4)


def breakout_score(bars: list[Bar]) -> float:
    """Price breaking above the session's prior high on expanding volume."""
    if len(bars) < 5:
        return 0.0
    prior = bars[:-1]
    last = bars[-1]
    prior_high = max(b.high for b in prior)
    if prior_high <= 0 or last.close < prior_high:
        return 0.0

    prior_vol = _avg(b.volume for b in prior)
    vol_expansion = min(last.volume / prior_vol, 3.0) / 3.0 if prior_vol > 0 else 0.5
    break_strength = min((last.close - prior_high) / prior_high / 0.02, 1.0)
    return round(break_strength * 0.6 + vol_expansion * 0.4, 4)


def detect_pattern(bars: list[Bar], symbol: str) -> PatternReport:
    """Score all patterns and report the strongest one found."""
    if not bars:
        return PatternReport(symbol.upper(), 0.5, None, "no bar data", has_data=False)

    scores = {
        "bull_flag": bull_flag_score(bars),
        "higher_lows": higher_lows_score(bars),
        "breakout": breakout_score(bars),
    }
    best_name = max(scores, key=scores.get)
    best = scores[best_name]
    if best <= 0.0:
        return PatternReport(symbol.upper(), 0.5, None, "no pattern detected", has_data=True)
    return PatternReport(symbol.upper(), best, best_name, f"{best_name} score {best}", has_data=True)


def pattern_score_for_symbol(
    broker: WebullSandboxBroker,
    symbol: str,
    *,
    timespan: str = "M5",
    bar_count: int = 30,
) -> PatternReport:
    """Fetch recent bars for a symbol and run pattern detection (5m default)."""
    try:
        response = broker.session.data_client.market_data.get_history_bar(
            symbol.upper(), US_STOCK_CATEGORY, timespan, count=str(bar_count)
        )
        bars = bars_from_response(response.json())
    except Exception:
        bars = []
    return detect_pattern(bars, symbol)


__all__ = [
    "Bar",
    "PatternReport",
    "bars_from_response",
    "breakout_score",
    "bull_flag_score",
    "detect_pattern",
    "higher_lows_score",
    "pattern_score_for_symbol",
]
