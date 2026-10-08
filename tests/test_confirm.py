from decimal import Decimal

from god_help_tradebot import MonitorLoop, PaperBroker, WebullSandboxBroker, WebullSdkSession
from god_help_tradebot.confirm import (
    analyst_rating_score,
    capital_flow_score,
    confirm_candidate,
)
from god_help_tradebot.news import NewsReport, fetch_news_sentiment
from god_help_tradebot.screen import MarketSnapshot
from datetime import datetime
from god_help_tradebot.schedule import EASTERN


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class _FakeFundamentals:
    def __init__(self, flow_record) -> None:
        self._flow_record = flow_record

    def get_capital_flow(self, symbol, category="US_STOCK", count=None):
        return _FakeResponse([self._flow_record] if self._flow_record else [])


class _FakeInstrument:
    def __init__(self, rating_record) -> None:
        self._rating_record = rating_record

    def get_analyst_rating(self, symbol, category="US_STOCK"):
        return _FakeResponse(self._rating_record)


class _FakeDataClient:
    def __init__(self, flow_record, rating_record) -> None:
        self.fundamentals = _FakeFundamentals(flow_record)
        self.instrument = _FakeInstrument(rating_record)


def _broker(flow_record, rating_record) -> WebullSandboxBroker:
    session = WebullSdkSession(
        trade_client=None,
        data_client=_FakeDataClient(flow_record, rating_record),
        account_id="cfg-1",
    )
    return WebullSandboxBroker(session=session)


def _flow(large_in, large_out, small_in=0, small_out=0, medium_in=0, medium_out=0):
    return {
        "large_in": str(large_in),
        "large_out": str(large_out),
        "medium_in": str(medium_in),
        "medium_out": str(medium_out),
        "small_in": str(small_in),
        "small_out": str(small_out),
    }


def _rating(strong_buy=0, buy=0, hold=0, sell=0, under_perform=0):
    return {
        "strong_buy": str(strong_buy),
        "buy": str(buy),
        "hold": str(hold),
        "sell": str(sell),
        "under_perform": str(under_perform),
    }


def test_capital_flow_scores_large_inflow() -> None:
    broker = _broker(_flow(600, 200), None)
    score, ratio, net = capital_flow_score(broker, "GOW")
    assert ratio == 0.75
    assert score == 0.75
    assert net == 400


def test_capital_flow_missing_is_neutral() -> None:
    broker = _broker(None, None)
    score, ratio, net = capital_flow_score(broker, "GOW")
    assert score == 0.5
    assert ratio is None


def test_analyst_rating_uses_dict_payload_and_underscore_field() -> None:
    broker = _broker(None, _rating(strong_buy=19, buy=6, hold=13, sell=3, under_perform=3))
    score, ratio = analyst_rating_score(broker, "AAPL")
    assert ratio == 25 / 44
    assert abs(score - 0.5682) < 1e-4


def test_analyst_rating_absent_is_neutral() -> None:
    broker = _broker(None, None)
    score, ratio = analyst_rating_score(broker, "GOW")
    assert score == 0.5
    assert ratio is None


def test_confirm_candidate_passes_strong_flow_and_ratings() -> None:
    broker = _broker(_flow(700, 300), _rating(strong_buy=8, buy=4, hold=3, sell=1))
    report = confirm_candidate(broker, "GOW", min_score=0.5)
    assert report.symbol == "GOW"
    assert report.passed is True
    assert report.score > 0.6


def test_confirm_candidate_rejects_weak_flow() -> None:
    broker = _broker(_flow(200, 800), _rating(strong_buy=0, buy=1, hold=2, sell=9))
    report = confirm_candidate(broker, "GOW", min_score=0.5)
    assert report.passed is False
    assert report.score < 0.5


def _open_market_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _snapshot(symbol: str) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        price=3.0,
        change_percent=5.0,
        volume=500_000,
        average_volume=100_000,
        bid=2.99,
        ask=3.01,
        day_low=2.80,
        day_high=3.20,
    )


def test_monitor_skips_unconfirmed_candidate_and_takes_next() -> None:
    broker = PaperBroker(10_000)

    def _confirmation(symbol: str):
        from god_help_tradebot.confirm import ConfirmationReport

        return ConfirmationReport(
            symbol=symbol,
            score=0.4 if symbol == "BAD" else 0.8,
            net_capital_flow=0,
            large_flow_ratio=None,
            analyst_buy_ratio=None,
            news_score=None,
            news_article_count=0,
            passed=symbol != "BAD",
        )

    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snapshot("BAD"), _snapshot("GOOD")],
        confirmation=_confirmation,
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries[0].symbol == "GOOD"
    assert any(e.kind == "confirmation" and e.symbol == "BAD" for e in summary.events)


def test_monitor_without_confirmation_takes_top_candidate() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snapshot("GOW")],
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries[0].symbol == "GOW"


class _FakeNewsResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("http error")


def _news_payload(items):
    return {"feed": items}


def _news_item(ticker: str, score: float, relevance: float = 1.0):
    return {
        "title": "headline",
        "ticker_sentiment": [
            {
                "ticker": ticker,
                "relevance_score": str(relevance),
                "ticker_sentiment_score": str(score),
            }
        ],
    }


def test_news_sentiment_scores_bullish_coverage(monkeypatch) -> None:
    items = [_news_item("GOW", 0.5), _news_item("GOW", 0.4), _news_item("GOW", 0.3)]
    monkeypatch.setattr(
        "god_help_tradebot.news.requests.get",
        lambda *a, **k: _FakeNewsResponse(_news_payload(items)),
    )

    report = fetch_news_sentiment("GOW")

    assert report.symbol == "GOW"
    assert report.article_count == 3
    assert report.bullish == 3
    assert report.average_sentiment == 0.4
    assert report.score > 0.5


def test_news_sentiment_scores_bearish_low(monkeypatch) -> None:
    items = [_news_item("GOW", -0.5), _news_item("GOW", -0.4)]
    monkeypatch.setattr(
        "god_help_tradebot.news.requests.get",
        lambda *a, **k: _FakeNewsResponse(_news_payload(items)),
    )

    report = fetch_news_sentiment("GOW")

    assert report.bearish == 2
    assert report.score < 0.5


def test_news_sentiment_ignores_other_tickers(monkeypatch) -> None:
    items = [_news_item("GOW", 0.5), _news_item("OTHER", -0.9)]
    monkeypatch.setattr(
        "god_help_tradebot.news.requests.get",
        lambda *a, **k: _FakeNewsResponse(_news_payload(items)),
    )

    report = fetch_news_sentiment("GOW")

    assert report.article_count == 1


def test_news_sentiment_network_failure_is_neutral(monkeypatch) -> None:
    def _raise(*a, **k):
        raise RuntimeError("offline")

    monkeypatch.setattr("god_help_tradebot.news.requests.get", _raise)

    report = fetch_news_sentiment("GOW")

    assert report.score == 0.5
    assert report.article_count == 0


def test_confirm_candidate_blends_news_into_score() -> None:
    broker = _broker(_flow(700, 300), _rating(strong_buy=8, buy=4, hold=3, sell=1))
    news = NewsReport("GOW", 0.9, 5, 0.4, 4, 0, 1)

    with_news = confirm_candidate(broker, "GOW", min_score=0.5, news_provider=lambda s: news)
    without_news = confirm_candidate(broker, "GOW", min_score=0.5, include_news=False)

    assert with_news.news_score == 0.9
    assert with_news.news_article_count == 5
    assert with_news.score != without_news.score
    assert with_news.passed is True


def test_confirm_candidate_news_failure_falls_back_to_two_signal_score() -> None:
    broker = _broker(_flow(700, 300), _rating(strong_buy=8, buy=4, hold=3, sell=1))

    def _failing_news(symbol: str):
        raise RuntimeError("news offline")

    report = confirm_candidate(broker, "GOW", min_score=0.5, news_provider=_failing_news)

    assert report.news_score is None
    assert report.score == round(((0.7 * 0.45) + (0.75 * 0.20)) / 0.65, 4)
