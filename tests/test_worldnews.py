from god_help_tradebot.worldnews import (
    MarketNewsReport,
    fetch_market_news,
    market_adjustment,
)


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


def _item(sentiment: float):
    return {"title": "macro headline", "overall_sentiment_score": str(sentiment)}


def test_market_news_scores_bullish_backdrop(monkeypatch) -> None:
    items = [_item(0.4), _item(0.3), _item(0.2), _item(-0.1)]
    monkeypatch.setattr(
        "god_help_tradebot.worldnews.requests.get",
        lambda *a, **k: _FakeResponse({"feed": items}),
    )

    report = fetch_market_news(api_key="k", use_cache=False)

    assert report.article_count == 4
    assert report.bullish == 3
    assert report.bearish == 0
    assert report.neutral == 1
    assert report.score > 0.5


def test_market_news_scores_bearish_backdrop(monkeypatch) -> None:
    items = [_item(-0.5), _item(-0.4)]
    monkeypatch.setattr(
        "god_help_tradebot.worldnews.requests.get",
        lambda *a, **k: _FakeResponse({"feed": items}),
    )

    report = fetch_market_news(api_key="k", use_cache=False)

    assert report.bearish == 2
    assert report.score < 0.5


def test_market_news_offline_is_neutral(monkeypatch) -> None:
    def _raise(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr("god_help_tradebot.worldnews.requests.get", _raise)

    report = fetch_market_news(api_key="k", use_cache=False)

    assert report.score == 0.5
    assert report.article_count == 0


def test_market_adjustment_favorable_lowers_bar() -> None:
    favorable = MarketNewsReport(0.7, 50, 0.4, 30, 5, 15)
    assert market_adjustment(favorable) > 0


def test_market_adjustment_hostile_raises_bar() -> None:
    hostile = MarketNewsReport(0.3, 50, -0.4, 5, 30, 15)
    assert market_adjustment(hostile) < 0


def test_market_adjustment_empty_is_zero() -> None:
    empty = MarketNewsReport(0.5, 0, None, 0, 0, 0)
    assert market_adjustment(empty) == 0.0


def test_market_news_uses_cache_within_ttl(monkeypatch) -> None:
    import god_help_tradebot.worldnews as worldnews

    worldnews._cache["at"] = 0.0
    worldnews._cache["report"] = None
    calls = []

    def _get(*a, **k):
        calls.append(1)
        return _FakeResponse({"feed": [_item(0.3)]})

    monkeypatch.setattr("god_help_tradebot.worldnews.requests.get", _get)

    first = fetch_market_news(api_key="k", use_cache=True)
    second = fetch_market_news(api_key="k", use_cache=True)

    assert len(calls) == 1
    assert first is second
