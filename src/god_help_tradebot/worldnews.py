"""Broad-market news layer: macro, monetary, financial-market, and world news.

Queries Alpha Vantage's NEWS_SENTIMENT endpoint by topic (not ticker) to gauge
the overall market backdrop — the kind of national/global news that moves whole
sectors. Produces a market-sentiment score that nudges the entry threshold up in
hostile conditions and down in favorable ones.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import requests

from .news import DEFAULT_TIMEOUT, NEWS_ENDPOINT, NEWS_FUNCTION, _env_api_key, _float

# Topics that capture broad market-moving coverage (macro, policy, world events).
GLOBAL_TOPICS = (
    "economy_macro",
    "economy_monetary",
    "economy_fiscal",
    "financial_markets",
    "finance",
)

# Cache the broad market backdrop briefly so rapid successive candidate checks
# don't hammer the rate-limited API for identical macro news.
_CACHE_TTL_SECONDS = 60
_cache: dict[str, Any] = {"at": 0.0, "report": None}


@dataclass(frozen=True)
class MarketNewsReport:
    score: float
    article_count: int
    average_sentiment: float | None
    bullish: int
    bearish: int
    neutral: int


def _label(score: float) -> str:
    if score >= 0.15:
        return "bullish"
    if score <= -0.15:
        return "bearish"
    return "neutral"


def fetch_market_news(
    *,
    api_key: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    limit: int = 50,
    use_cache: bool = True,
) -> MarketNewsReport:
    """Fetch broad market/macro/world news and score the overall backdrop."""
    now = time.monotonic()
    cached = _cache.get("report")
    if use_cache and cached is not None and now - _cache["at"] < _CACHE_TTL_SECONDS:
        return cached

    key = api_key or _env_api_key() or "demo"

    try:
        response = requests.get(
            NEWS_ENDPOINT,
            params={
                "function": NEWS_FUNCTION,
                "topics": ",".join(GLOBAL_TOPICS),
                "apikey": key,
                "limit": str(limit),
                "sort": "RELEVANCE",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return MarketNewsReport(0.5, 0, None, 0, 0, 0)

    feed = payload.get("feed")
    if not isinstance(feed, list):
        return MarketNewsReport(0.5, 0, None, 0, 0, 0)

    scores: list[float] = []
    bullish = bearish = neutral = 0
    for item in feed:
        if not isinstance(item, dict):
            continue
        score = _float(item.get("overall_sentiment_score"))
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
        return MarketNewsReport(0.5, 0, None, 0, 0, 0)

    average = sum(scores) / count
    # Map [-1, 1] to [0, 1]; 0.5 is a neutral backdrop.
    score = round(min(max((average + 1.0) / 2.0, 0.0), 1.0), 4)

    report = MarketNewsReport(
        score=score,
        article_count=count,
        average_sentiment=round(average, 4),
        bullish=bullish,
        bearish=bearish,
        neutral=neutral,
    )
    _cache["at"] = now
    _cache["report"] = report
    return report


def market_adjustment(report: MarketNewsReport, *, strength: float = 0.05) -> float:
    """Translate the market backdrop into a small threshold adjustment.

    A favorable backdrop (score > 0.5) lowers the bar slightly; a hostile one
    raises it. Bounded so macro news nudges rather than dominates the decision.
    """
    if report.article_count == 0:
        return 0.0
    return round((report.score - 0.5) * 2 * strength, 4)


__all__ = ["MarketNewsReport", "fetch_market_news", "market_adjustment", "GLOBAL_TOPICS"]
