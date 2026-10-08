from god_help_tradebot import BotConfig, WebullSdkSession, WebullSandboxBroker
from god_help_tradebot.confirm import confirm_candidate
from god_help_tradebot.patterns import (
    Bar,
    bars_from_response,
    breakout_score,
    bull_flag_score,
    detect_pattern,
    higher_lows_score,
)


def _bar(o, h, l, c, v, t="t"):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def test_bars_from_response_unwraps_nested_result() -> None:
    payload = {
        "result": [
            {"result": [
                {"time": "t2", "open": "1.1", "high": "1.2", "low": "1.05", "close": "1.15", "volume": "100"},
                {"time": "t1", "open": "1.0", "high": "1.1", "low": "0.95", "close": "1.05", "volume": "50"},
            ]}
        ]
    }

    bars = bars_from_response(payload)

    assert len(bars) == 2
    assert bars[0].time == "t1"  # chronological order restored
    assert bars[0].close == 1.05


def test_bars_from_response_empty() -> None:
    assert bars_from_response({"result": []}) == []
    assert bars_from_response(None) == []


def test_bull_flag_detects_spike_and_tight_flag() -> None:
    bars = [
        _bar(1.00, 1.02, 0.99, 1.01, 100), _bar(1.01, 1.05, 1.00, 1.04, 150),
        _bar(1.04, 1.10, 1.03, 1.09, 300), _bar(1.09, 1.15, 1.08, 1.14, 400),
        # Tight, low-volume consolidation holding the gain:
        _bar(1.14, 1.15, 1.12, 1.13, 100), _bar(1.13, 1.14, 1.12, 1.13, 80),
        _bar(1.13, 1.14, 1.12, 1.13, 90), _bar(1.13, 1.14, 1.12, 1.13, 85),
    ]

    assert bull_flag_score(bars) > 0.4


def test_bull_flag_rejects_flat_price() -> None:
    bars = [_bar(1.0, 1.01, 0.99, 1.0, 100) for _ in range(8)]
    assert bull_flag_score(bars) == 0.0


def test_higher_lows_detects_stair_step() -> None:
    bars = [
        _bar(1.0, 1.05, 0.95, 1.0, 100), _bar(1.0, 1.06, 0.97, 1.02, 100),
        _bar(1.02, 1.07, 0.99, 1.04, 100), _bar(1.04, 1.08, 1.01, 1.06, 100),
        _bar(1.06, 1.09, 1.03, 1.08, 100), _bar(1.08, 1.10, 1.05, 1.09, 100),
    ]

    assert higher_lows_score(bars) > 0.6


def test_breakout_detects_high_break_with_volume() -> None:
    bars = [
        _bar(1.0, 1.10, 0.98, 1.05, 100), _bar(1.05, 1.10, 1.0, 1.06, 100),
        _bar(1.06, 1.10, 1.02, 1.05, 100), _bar(1.05, 1.09, 1.0, 1.04, 100),
        _bar(1.04, 1.15, 1.04, 1.14, 500),  # breaks prior 1.10 high on 5x volume
    ]

    assert breakout_score(bars) > 0.5


def test_breakout_rejects_below_prior_high() -> None:
    bars = [
        _bar(1.0, 1.10, 0.98, 1.05, 100), _bar(1.05, 1.10, 1.0, 1.06, 100),
        _bar(1.06, 1.10, 1.02, 1.05, 100), _bar(1.05, 1.09, 1.0, 1.04, 100),
        _bar(1.04, 1.08, 1.03, 1.06, 500),
    ]

    assert breakout_score(bars) == 0.0


def test_detect_pattern_no_data_is_neutral() -> None:
    report = detect_pattern([], "GOW")
    assert report.has_data is False
    assert report.score == 0.5
    assert report.pattern is None


def test_detect_pattern_reports_best_pattern() -> None:
    bars = [
        _bar(1.0, 1.05, 0.95, 1.0, 100), _bar(1.0, 1.06, 0.97, 1.02, 100),
        _bar(1.02, 1.07, 0.99, 1.04, 100), _bar(1.04, 1.08, 1.01, 1.06, 100),
        _bar(1.06, 1.09, 1.03, 1.08, 100), _bar(1.08, 1.10, 1.05, 1.09, 100),
    ]

    report = detect_pattern(bars, "GOW")

    assert report.has_data is True
    assert report.pattern in ("bull_flag", "higher_lows", "breakout")
    assert report.score > 0


class _FakeResponse:
    def __init__(self, payload) -> None:
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


class _FakeFundamentals:
    def get_capital_flow(self, symbol, category="US_STOCK", count=None):
        return _FakeResponse([])


class _FakeInstrument:
    def get_analyst_rating(self, symbol, category="US_STOCK"):
        return _FakeResponse({})


class _FakeDataClient:
    def __init__(self) -> None:
        self.fundamentals = _FakeFundamentals()
        self.instrument = _FakeInstrument()


def _broker() -> WebullSandboxBroker:
    session = WebullSdkSession(trade_client=None, data_client=_FakeDataClient(), account_id="cfg-1")
    return WebullSandboxBroker(session=session)


def test_confirm_blends_pattern_when_present() -> None:
    from god_help_tradebot.patterns import PatternReport

    broker = _broker()
    pattern = PatternReport("GOW", 0.9, "bull_flag", "bull_flag score 0.9", has_data=True)

    with_pattern = confirm_candidate(
        broker, "GOW", include_news=False, pattern_provider=lambda s: pattern
    )
    without_pattern = confirm_candidate(broker, "GOW", include_news=False, include_patterns=False)

    assert with_pattern.pattern_score == 0.9
    assert with_pattern.pattern_name == "bull_flag"
    assert with_pattern.score != without_pattern.score


def test_confirm_ignores_pattern_without_data() -> None:
    from god_help_tradebot.patterns import PatternReport

    broker = _broker()
    pattern = PatternReport("GOW", 0.5, None, "no bar data", has_data=False)

    report = confirm_candidate(broker, "GOW", include_news=False, pattern_provider=lambda s: pattern)

    assert report.pattern_score is None
