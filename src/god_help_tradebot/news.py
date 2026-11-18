"""News sentiment layer for candidate confirmation.

Queries Alpha Vantage's NEWS_SENTIMENT endpoint (free demo key) for a symbol's
recent coverage and converts per-ticker sentiment into a 0-1 score. Coverage
breadth matters: more relevant recent articles with consistent sentiment score
higher. Network or API failures degrade gracefully to neutral.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any

import requests

NEWS_ENDPOINT = "https://www.alphavantage.co/query"
NEWS_FUNCTION = "NEWS_SENTIMENT"
DEFAULT_TIMEOUT = 10

# Alpha Vantage's free tier allows ~25 requests/day. Real reports are cached
# per symbol for the whole UTC day; empty/degraded results recheck after a
# short TTL so a transient blip doesn't freeze a symbol's news all day.
_NEWS_CACHE: dict[str, tuple[float, "NewsReport"]] = {}
_EMPTY_REPORT_TTL_SECONDS = 600
_NEWS_LOOKBACK = timedelta(hours=72)
_NEWS_TIME_FORMAT = "%Y%m%dT%H%M%S"


@dataclass(frozen=True)
class NewsReport:
    symbol: str
    score: float
    article_count: int
    average_sentiment: float | None
    bullish: int
    bearish: int
    neutral: int


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _label(score: float) -> str:
    if score >= 0.15:
        return "bullish"
    if score <= -0.15:
        return "bearish"
    return "neutral"


def _env_api_key() -> str | None:
    """Read ALPHAVANTAGE_API_KEY from the process environment or a repo-root .env."""
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if key:
        return key
    path = Path.cwd() / ".env"
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ALPHAVANTAGE_API_KEY="):
            return line.split("=", 1)[1].strip().strip("\"'")
    return None


def fetch_news_sentiment(
    symbol: str,
    *,
    api_key: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> NewsReport:
    """Fetch recent news sentiment for a symbol from Alpha Vantage.

    Uses the ALPHAVANTAGE_API_KEY from the environment or .env when set; otherwise
    falls back to the limited public demo key (IBM/AAPL coverage only).
    """
    symbol = symbol.upper()
    key = api_key or _env_api_key() or "demo"

    requested_at = datetime.now(timezone.utc)
    earliest = requested_at - _NEWS_LOOKBACK
    cache_key = f"{symbol}:{requested_at.date().isoformat()}"
    now = time.time()
    cached = _NEWS_CACHE.get(cache_key)
    if cached is not None:
        cached_at, cached_report = cached
        if cached_report.article_count > 0 or now - cached_at < _EMPTY_REPORT_TTL_SECONDS:
            return cached_report

    try:
        response = requests.get(
            NEWS_ENDPOINT,
            params={
                "function": NEWS_FUNCTION,
                "tickers": symbol,
                "apikey": key,
                "limit": "50",
                "time_from": earliest.strftime("%Y%m%dT%H%M"),
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        report = NewsReport(symbol, 0.5, 0, None, 0, 0, 0)
        _NEWS_CACHE[cache_key] = (now, report)
        return report

    feed = payload.get("feed") if isinstance(payload, dict) else None
    if not isinstance(feed, list):
        report = NewsReport(symbol, 0.5, 0, None, 0, 0, 0)
        _NEWS_CACHE[cache_key] = (now, report)
        return report

    scores: list[float] = []
    bullish = bearish = neutral = 0
    for item in feed:
        if not isinstance(item, dict):
            continue
        try:
            published = datetime.strptime(item.get("time_published"), _NEWS_TIME_FORMAT).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if not earliest <= published <= requested_at:
            continue
        for entry in item.get("ticker_sentiment", []) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("ticker", "")).upper() != symbol:
                continue
            score = _float(entry.get("ticker_sentiment_score"))
            if score is None or not isfinite(score) or not -1 <= score <= 1:
                continue
            scores.append(score)
            label = _label(score)
            if label == "bullish":
                bullish += 1
            elif label == "bearish":
                bearish += 1
            else:
                neutral += 1

    count = len(scores)
    if count == 0:
        report = NewsReport(symbol, 0.5, 0, None, 0, 0, 0)
        _NEWS_CACHE[cache_key] = (now, report)
        return report

    average = sum(scores) / count
    coverage_component = min(count / 10.0, 1.0)
    score = round(0.5 + (average / 2.0) * (0.7 + 0.3 * coverage_component), 4)

    report = NewsReport(
        symbol=symbol,
        score=min(max(score, 0.0), 1.0),
        article_count=count,
        average_sentiment=round(average, 4),
        bullish=bullish,
        bearish=bearish,
        neutral=neutral,
    )
    _NEWS_CACHE[cache_key] = (now, report)
    return report


__all__ = ["NewsReport", "fetch_news_sentiment"]
