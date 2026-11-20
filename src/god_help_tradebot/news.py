"""News sentiment layer for candidate confirmation.

Queries Alpha Vantage's NEWS_SENTIMENT endpoint (free demo key) for a symbol's
recent coverage and converts per-ticker sentiment into a 0-1 score. Coverage
breadth matters: more relevant recent articles with consistent sentiment score
higher. Network or API failures degrade gracefully to neutral.
"""

from __future__ import annotations

import os
import time as wall_time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import isfinite
from pathlib import Path
from typing import Any

import requests
import re

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


@dataclass(frozen=True)
class ExchangeClosureNotice:
    closed_date: date
    early_close: time | None
    source: str


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


EXCHANGE_CLOSURE_LOOKBACK = timedelta(days=14)
_MARKET_CLOSURE_CACHE: dict[str, tuple[float, list[ExchangeClosureNotice]]] = {}
_MARKET_CLOSURE_TTL_SECONDS = 3600


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


def fetch_exchange_closure_notices(
    *, api_key: str | None = None, timeout: int = DEFAULT_TIMEOUT,
) -> list[ExchangeClosureNotice]:
    key = api_key or _env_api_key() or "demo"
    now = datetime.now(timezone.utc)
    cache_key = f"closures:{now.date().isoformat()}"
    cached = _MARKET_CLOSURE_CACHE.get(cache_key)
    if cached is not None and wall_time.time() - cached[0] < _MARKET_CLOSURE_TTL_SECONDS:
        return cached[1]

    earliest = now - EXCHANGE_CLOSURE_LOOKBACK
    try:
        response = requests.get(
            NEWS_ENDPOINT,
            params={
                "function": NEWS_FUNCTION,
                "topics": "financial_markets,economy_macro",
                "apikey": key,
                "limit": "100",
                "time_from": earliest.strftime("%Y%m%dT%H%M"),
                "sort": "LATEST",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        _MARKET_CLOSURE_CACHE[cache_key] = (wall_time.time(), [])
        return []

    feed = payload.get("feed") if isinstance(payload, dict) else None
    if not isinstance(feed, list):
        _MARKET_CLOSURE_CACHE[cache_key] = (wall_time.time(), [])
        return []

    notices: list[ExchangeClosureNotice] = []
    iso_pattern = re.compile(r"\b(202[6-9]|203[0-2])-(\d{2})-(\d{2})\b")
    month_names = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
    month_pattern = re.compile(rf"\b({'|'.join(month_names)})\s+(\d{{1,2}})(?:st|nd|rd|th)?(?:,)?\s+(202[6-9]|203[0-2])\b", re.IGNORECASE)

    for item in feed:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get(field, "") or "") for field in ("title", "summary", "source"))
        lowered = text.lower()
        url = str(item.get("url") or "").lower()
        source_name = str(item.get("source") or "").lower()
        official_source = (
            "nyse.com" in url or "nasdaq.com" in url
            or "official nyse" in source_name or "official nasdaq" in source_name
        )
        if not official_source:
            continue
        if not ("nyse" in lowered or "nasdaq" in lowered or "u.s. stock market" in lowered or "us stock market" in lowered):
            continue
        if not re.search(r"\b(?:closed|close early|early close|closes early|market closure|will remain closed|will be closed|not open)\b", lowered):
            continue

        parsed_date = None
        match = iso_pattern.search(text)
        if match:
            try:
                parsed_date = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                parsed_date = None
        if parsed_date is None:
            match = month_pattern.search(text)
            if match:
                month = month_names.index(match.group(1).title()) + 1
                try:
                    parsed_date = date(int(match.group(3)), month, int(match.group(2)))
                except ValueError:
                    parsed_date = None
        if parsed_date is None:
            continue

        early_close = time(13, 0) if re.search(r"\b(?:early close|close early|closes early|1:00\s*p\.?m\.?)\b", lowered) else None
        if re.search(r"\b(?:closed all day|markets? closed|market closure|will remain closed|will be closed|not open)\b", lowered):
            early_close = None
        source = str(item.get("url") or item.get("title") or "news source")
        notices.append(ExchangeClosureNotice(parsed_date, early_close, source[:300]))

    unique: dict[tuple[date, time | None, str], ExchangeClosureNotice] = {
        (notice.closed_date, notice.early_close, notice.source): notice for notice in notices
    }
    result = list(unique.values())
    _MARKET_CLOSURE_CACHE[cache_key] = (wall_time.time(), result)
    return result


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
    now = wall_time.time()
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
