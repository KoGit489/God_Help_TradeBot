"""Wide-net mode: bigger lists, daily news cache, per-cycle confirmation budget."""

from datetime import datetime
from types import SimpleNamespace

from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker, WebullSdkSession, WebullSandboxBroker
from god_help_tradebot.autoscreen import fetch_top_gainers
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.screen import MarketSnapshot


def _open_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


# --- Screener list limit -------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload

    def json(self):
        return self._payload


class _FakeScreener:
    def __init__(self, rows) -> None:
        self._rows = rows

    def list_gainers_losers(self, rank_type, category, sort_by, direction=None):
        return _FakeResponse(self._rows)


class _FakeDataClient:
    def __init__(self, rows) -> None:
        self.screener = _FakeScreener(rows)


def _broker(rows) -> WebullSandboxBroker:
    session = WebullSdkSession(trade_client=None, data_client=_FakeDataClient(rows), account_id="cfg-1")
    return WebullSandboxBroker(session=session)


def test_screener_list_limit_controls_net_width() -> None:
    rows = [{"symbol": f"S{i}", "price": "3.0"} for i in range(8)]
    broker = _broker(rows)

    assert len(fetch_top_gainers(broker, limit=3)) == 3
    assert len(fetch_top_gainers(broker, limit=100)) == 8


# --- Alpha Vantage daily news cache ----------------------------------------------


def test_news_is_cached_per_symbol_per_day(monkeypatch) -> None:
    import god_help_tradebot.news as news

    news._NEWS_CACHE.clear()
    calls = []

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"feed": [{"ticker_sentiment": [{"ticker": "XYZ", "ticker_sentiment_score": "0.2"}]}]}

    def _fake_get(*args, **kwargs):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(news.requests, "get", _fake_get)

    first = news.fetch_news_sentiment("xyz", api_key="TESTKEY")
    second = news.fetch_news_sentiment("XYZ", api_key="TESTKEY")
    news.fetch_news_sentiment("ABC", api_key="TESTKEY")

    assert len(calls) == 2  # one HTTP call per symbol, not per request
    assert first == second
    assert first.article_count == 1


def test_empty_news_result_is_cached_briefly(monkeypatch) -> None:
    import god_help_tradebot.news as news

    news._NEWS_CACHE.clear()
    calls = []

    class _Resp:
        def raise_for_status(self) -> None:
            pass

        def json(self):
            return {"feed": []}

    def _fake_get(*args, **kwargs):
        calls.append(1)
        return _Resp()

    monkeypatch.setattr(news.requests, "get", _fake_get)

    news.fetch_news_sentiment("EMPTY", api_key="TESTKEY")
    news.fetch_news_sentiment("EMPTY", api_key="TESTKEY")

    assert len(calls) == 1  # empty results recheck only after the short TTL


# --- Confirmation budget ---------------------------------------------------------


def _candidate(index: int) -> MarketSnapshot:
    return MarketSnapshot(f"C{index}", 3.0, 5.0, 500_000, 100_000, 2.99, 3.01, 2.80, 3.20)


def test_confirmation_budget_caps_deep_checks_per_cycle() -> None:
    candidates = [_candidate(i) for i in range(8)]
    calls = []

    def _reject(symbol: str) -> SimpleNamespace:
        calls.append(symbol)
        return SimpleNamespace(score=0.1, passed=False, signals_present=3)

    loop = MonitorLoop(
        PaperBroker(10_000),
        BotConfig(max_confirmations_per_cycle=3, allow_fallback=False),
        confirmation=_reject,
        snapshot_provider=lambda: candidates,
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert len(calls) == 3  # budget spent, walking stopped
    assert summary.entries == []
    assert any(e.kind == "confirmation_budget" for e in summary.events)

    loop.run_once(summary)
    assert len(calls) == 6  # fresh budget next cycle — nothing starves
