"""News sentiment layer for candidate confirmation.

Queries Alpha Vantage's NEWS_SENTIMENT endpoint (free demo key) for a symbol's
recent coverage and converts per-ticker sentiment into a 0-1 score. Coverage
breadth matters: more relevant recent articles with consistent sentiment score
higher. Network or API failures degrade gracefully to neutral.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

NEWS_ENDPOINT = "https://www.alphavantage.co/query"
NEWS_FUNCTION = "NEWS_SENTIMENT"
DEFAULT_TIMEOUT = 10


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

    try:
        response = requests.get(
            NEWS_ENDPOINT,
            params={
                "function": NEWS_FUNCTION,
                "tickers": symbol,
                "apikey": key,
                "limit": "50",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return NewsReport(symbol, 0.5, 0, None, 0, 0, 0)

    feed = payload.get("feed")
    if not isinstance(feed, list):
        return NewsReport(symbol, 0.5, 0, None, 0, 0, 0)

    scores: list[float] = []
    bullish = bearish = neutral = 0
    for item in feed:
        if not isinstance(item, dict):
            continue
        for entry in item.get("ticker_sentiment", []) or []:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("ticker", "")).upper() != symbol:
                continue
            score = _float(entry.get("ticker_sentiment_score"))
            if score is None:
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
        return NewsReport(symbol, 0.5, 0, None, 0, 0, 0)

    average = sum(scores) / count
    # Map sentiment from [-1, 1] to [0, 1].
    sentiment_component = (average + 1.0) / 2.0
    # Coverage breadth rewards more reporting, saturating around 10 articles.
    coverage_component = min(count / 10.0, 1.0)
    # Blend: sentiment matters most, but more coverage strengthens the signal.
    score = round((sentiment_component * 0.7) + (coverage_component * 0.3 * sentiment_component), 4)

    return NewsReport(
        symbol=symbol,
        score=min(max(score, 0.0), 1.0),
        article_count=count,
        average_sentiment=round(average, 4),
        bullish=bullish,
        bearish=bearish,
        neutral=neutral,
    )


__all__ = ["NewsReport", "fetch_news_sentiment"]
