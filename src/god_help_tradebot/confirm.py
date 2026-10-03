"""Confirmation signals that solidify a screened candidate before entry.

Pulls additional Webull OpenAPI data — capital flow (money in/out) and analyst
ratings — and combines them into a confirmation score. Used after the price,
volume, spread, and range filters pass, so only strong candidates reach entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .broker import WebullSandboxBroker, US_STOCK_CATEGORY
from .news import NewsReport, fetch_news_sentiment


@dataclass(frozen=True)
class ConfirmationReport:
    symbol: str
    score: float
    net_capital_flow: float
    large_flow_ratio: float | None
    analyst_buy_ratio: float | None
    news_score: float | None = None
    news_article_count: int = 0
    passed: bool = False


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _records(response: Any) -> list[dict[str, Any]]:
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("data", [])
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _first_record(response: Any) -> dict[str, Any] | None:
    """Return the first record whether the payload is a list or a single dict."""
    payload = response.json()
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            return data[0] if data else None
        # Some endpoints return the record itself without a data wrapper.
        return payload if payload else None
    if isinstance(payload, list):
        return payload[0] if payload and isinstance(payload[0], dict) else None
    return None


def capital_flow_score(broker: WebullSandboxBroker, symbol: str) -> tuple[float, float | None, float]:
    """Score capital flow: large (institutional) inflow outweighs outflow.

    Returns (score 0-1, large_flow_ratio, net_flow). Large flow ratio is the
    share of large inflow within total large flow; None when unavailable.
    """
    try:
        records = _records(broker.session.data_client.fundamentals.get_capital_flow(symbol, US_STOCK_CATEGORY, count=1))
    except Exception:
        return 0.5, None, 0.0
    if not records:
        return 0.5, None, 0.0

    record = records[0]
    large_in = _float(record.get("large_in"))
    large_out = _float(record.get("large_out"))
    medium_in = _float(record.get("medium_in"))
    medium_out = _float(record.get("medium_out"))
    small_in = _float(record.get("small_in"))
    small_out = _float(record.get("small_out"))

    total_large = large_in + large_out
    large_ratio = (large_in / total_large) if total_large > 0 else None
    net_flow = (large_in + medium_in + small_in) - (large_out + medium_out + small_out)

    if large_ratio is None:
        return 0.5, None, net_flow
    # Map 0..1 ratio onto a 0..1 score where 0.5+ (more in than out) scores above half.
    return round(min(max(large_ratio, 0.0), 1.0), 4), large_ratio, net_flow


def analyst_rating_score(broker: WebullSandboxBroker, symbol: str) -> tuple[float, float | None]:
    """Score analyst ratings: share of buy/strong-buy ratings, if any exist.

    Returns (score 0-1, buy_ratio). Penny stocks often have no coverage;
    absence of ratings is neutral (0.5), not a penalty.
    """
    try:
        response = broker.session.data_client.instrument.get_analyst_rating(symbol, US_STOCK_CATEGORY)
        rating = _first_record(response)
    except Exception:
        return 0.5, None
    if rating is None:
        return 0.5, None

    buy = _float(rating.get("buy")) + _float(rating.get("strong_buy"))
    hold = _float(rating.get("hold"))
    sell = _float(rating.get("sell")) + _float(rating.get("strong_sell")) + _float(rating.get("under_perform")) + _float(rating.get("underperform"))
    total = buy + hold + sell
    if total <= 0:
        return 0.5, None
    buy_ratio = buy / total
    return round(min(max(buy_ratio, 0.0), 1.0), 4), buy_ratio


def confirm_candidate(
    broker: WebullSandboxBroker,
    symbol: str,
    *,
    min_score: float = 0.5,
    include_news: bool = True,
    news_provider: Callable[[str], NewsReport] | None = None,
) -> ConfirmationReport:
    """Combine flow, analyst, and news signals into a single pass/fail score."""
    flow_score, large_ratio, net_flow = capital_flow_score(broker, symbol)
    rating_score, buy_ratio = analyst_rating_score(broker, symbol)

    news_score: float | None = None
    news_articles = 0
    if include_news:
        provider = news_provider or fetch_news_sentiment
        try:
            report = provider(symbol)
            news_score = report.score
            news_articles = report.article_count
        except Exception:
            news_score = None

    if news_score is not None:
        # Flow remains the anchor; analyst coverage and news coverage add
        # independent confirmation. News carries the least weight because
        # penny-stock coverage is sparse and noisy.
        score = round((flow_score * 0.55) + (rating_score * 0.25) + (news_score * 0.20), 4)
    else:
        score = round((flow_score * 0.7) + (rating_score * 0.3), 4)

    return ConfirmationReport(
        symbol=symbol.upper(),
        score=score,
        net_capital_flow=round(net_flow, 2),
        large_flow_ratio=large_ratio,
        analyst_buy_ratio=buy_ratio,
        news_score=news_score,
        news_article_count=news_articles,
        passed=score >= min_score,
    )


__all__ = [
    "ConfirmationReport",
    "analyst_rating_score",
    "capital_flow_score",
    "confirm_candidate",
]
