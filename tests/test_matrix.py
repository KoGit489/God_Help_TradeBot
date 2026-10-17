from datetime import datetime
from decimal import Decimal

from god_help_tradebot import BotConfig, MonitorLoop, OrderSide, PaperBroker, Quote
from god_help_tradebot.confirm import confirm_candidate_bearish
from god_help_tradebot.patterns import Bar, bear_flag_score, breakdown_score, detect_bear_pattern, lower_highs_score
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.screen import MarketSnapshot, screen_loser_candidates


def _open_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _loser_snapshot(symbol: str, price: float, change: float, day_low: float, day_high: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        price=price,
        change_percent=change,
        volume=500_000,
        average_volume=100_000,
        bid=price - 0.01,
        ask=price + 0.01,
        day_low=day_low,
        day_high=day_high,
    )


# --- Loser screening -----------------------------------------------------------

def test_loser_screen_finds_hard_slider_near_low() -> None:
    snapshot = _loser_snapshot("DOWN", 2.0, -25.0, 1.95, 3.00)  # range_position ~0.05
    ranked = screen_loser_candidates([snapshot], BotConfig())

    assert len(ranked) == 1
    assert ranked[0].score > 0.5


def test_loser_screen_rejects_up_movers() -> None:
    snapshot = _loser_snapshot("UP", 2.0, 5.0, 1.95, 2.10)
    assert screen_loser_candidates([snapshot], BotConfig()) == []


def test_loser_screen_rejects_bounced_off_low() -> None:
    snapshot = _loser_snapshot("BOUNCE", 2.5, -25.0, 1.95, 3.00)  # range_position ~0.52
    assert screen_loser_candidates([snapshot], BotConfig()) == []


# --- Bearish chart patterns -------------------------------------------------------

def _bar(o, h, l, c, v, t="t"):
    return Bar(time=t, open=o, high=h, low=l, close=c, volume=v)


def test_lower_highs_detects_stair_step_down() -> None:
    bars = [
        _bar(3.0, 3.10, 2.95, 3.0, 100), _bar(2.95, 3.05, 2.90, 2.95, 100),
        _bar(2.90, 3.00, 2.85, 2.90, 100), _bar(2.85, 2.95, 2.80, 2.85, 100),
        _bar(2.80, 2.90, 2.75, 2.80, 100), _bar(2.75, 2.85, 2.70, 2.75, 100),
    ]

    assert lower_highs_score(bars) > 0.6


def test_breakdown_detects_low_break_with_volume() -> None:
    bars = [
        _bar(3.0, 3.05, 2.90, 2.95, 100), _bar(2.95, 3.0, 2.90, 2.94, 100),
        _bar(2.94, 2.98, 2.90, 2.93, 100), _bar(2.93, 2.96, 2.90, 2.92, 100),
        _bar(2.92, 2.93, 2.80, 2.81, 500),  # breaks prior 2.90 low on 5x volume
    ]

    assert breakdown_score(bars) > 0.5


def test_bear_flag_detects_drop_and_tight_flag() -> None:
    bars = [
        _bar(3.00, 3.02, 2.98, 2.99, 100), _bar(2.99, 3.00, 2.90, 2.92, 150),
        _bar(2.92, 2.94, 2.80, 2.82, 300), _bar(2.82, 2.84, 2.70, 2.72, 400),
        _bar(2.72, 2.74, 2.70, 2.72, 100), _bar(2.72, 2.73, 2.71, 2.72, 80),
        _bar(2.72, 2.73, 2.71, 2.72, 90), _bar(2.72, 2.73, 2.71, 2.72, 85),
    ]

    assert bear_flag_score(bars) > 0.4


def test_detect_bear_pattern_reports_best() -> None:
    bars = [
        _bar(3.0, 3.10, 2.95, 3.0, 100), _bar(2.95, 3.05, 2.90, 2.95, 100),
        _bar(2.90, 3.00, 2.85, 2.90, 100), _bar(2.85, 2.95, 2.80, 2.85, 100),
        _bar(2.80, 2.90, 2.75, 2.80, 100), _bar(2.75, 2.85, 2.70, 2.75, 100),
    ]

    report = detect_bear_pattern(bars, "DOWN")

    assert report.has_data is True
    assert report.pattern == "lower_highs"
    assert report.score > 0


# --- Bearish confirmation ---------------------------------------------------------

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


def _broker(flow_record, rating_record):
    from god_help_tradebot import WebullSandboxBroker, WebullSdkSession

    session = WebullSdkSession(
        trade_client=None,
        data_client=_FakeDataClient(flow_record, rating_record),
        account_id="cfg-1",
    )
    return WebullSandboxBroker(session=session)


def test_bearish_confirmation_scores_outflow_and_sell_ratings() -> None:
    flow = {"large_in": "200", "large_out": "800", "medium_in": "0", "medium_out": "0", "small_in": "0", "small_out": "0"}
    rating = {"strong_buy": "0", "buy": "1", "hold": "2", "sell": "9", "under_perform": "0"}
    broker = _broker(flow, rating)

    report = confirm_candidate_bearish(
        broker, "DOWN", include_news=False, include_patterns=False
    )

    assert report.score > 0.6
    assert report.passed is True


def test_bearish_confirmation_weak_on_inflow() -> None:
    flow = {"large_in": "800", "large_out": "200", "medium_in": "0", "medium_out": "0", "small_in": "0", "small_out": "0"}
    rating = {"strong_buy": "9", "buy": "3", "hold": "2", "sell": "1", "under_perform": "0"}
    broker = _broker(flow, rating)

    report = confirm_candidate_bearish(
        broker, "UP", include_news=False, include_patterns=False
    )

    assert report.score < 0.5
    assert report.passed is False


# --- The full direction matrix -----------------------------------------------------

def test_matrix_negative_indicators_short_with_grain() -> None:
    broker = PaperBroker(10_000)
    broker.last_quotes["DOWN"] = Quote("DOWN", Decimal("1.99"), Decimal("2.01"), Decimal("2.0"))
    snapshot = _loser_snapshot("DOWN", 2.0, -25.0, 1.95, 3.00)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=True),
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].side is OrderSide.SHORT
    assert "[loser]" in summary.events[0].detail
    assert loop.exit_plans["DOWN"].direction == "short"


def test_matrix_positive_indicators_buy_with_grain() -> None:
    broker = PaperBroker(10_000)
    broker.last_quotes["UP"] = Quote("UP", Decimal("2.99"), Decimal("3.01"), Decimal("3.0"))
    snapshot = MarketSnapshot("UP", 3.0, 5.0, 500_000, 100_000, 2.99, 3.01, 2.80, 3.20)
    loop = MonitorLoop(
        broker,
        BotConfig(),
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].side is OrderSide.BUY


def test_matrix_loser_skipped_without_shorts_enabled() -> None:
    broker = PaperBroker(10_000)
    snapshot = _loser_snapshot("DOWN", 2.0, -25.0, 1.95, 3.00)
    loop = MonitorLoop(
        broker,
        BotConfig(),  # no shorts, no flip
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    # Loser isn't ranked without shorts, and it's not a bullish candidate either.
    assert all(order.symbol != "DOWN" for order in summary.entries)


# --- Accuracy gates ---------------------------------------------------------------

def test_quorum_rejects_candidate_with_too_few_signals() -> None:
    from god_help_tradebot.confirm import ConfirmationReport

    broker = PaperBroker(10_000)
    broker.last_quotes["UP"] = Quote("UP", Decimal("2.99"), Decimal("3.01"), Decimal("3.0"))
    snapshot = MarketSnapshot("UP", 3.0, 5.0, 500_000, 100_000, 2.99, 3.01, 2.80, 3.20)

    # Score passes but only one signal group has real data.
    thin = ConfirmationReport(
        symbol="UP", score=0.8, net_capital_flow=0, large_flow_ratio=0.7,
        analyst_buy_ratio=None, signals_present=1, passed=True,
    )
    loop = MonitorLoop(
        broker,
        BotConfig(min_signals_present=2, allow_fallback=False),
        snapshot_provider=lambda: [snapshot],
        confirmation=lambda symbol: thin,
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries == []
    assert any(e.kind == "insufficient_signals" for e in summary.events)


def test_quorum_allows_candidate_with_enough_signals() -> None:
    from god_help_tradebot.confirm import ConfirmationReport

    broker = PaperBroker(10_000)
    broker.last_quotes["UP"] = Quote("UP", Decimal("2.99"), Decimal("3.01"), Decimal("3.0"))
    snapshot = MarketSnapshot("UP", 3.0, 5.0, 500_000, 100_000, 2.99, 3.01, 2.80, 3.20)

    rich = ConfirmationReport(
        symbol="UP", score=0.8, net_capital_flow=0, large_flow_ratio=0.7,
        analyst_buy_ratio=0.6, signals_present=2, passed=True,
    )
    loop = MonitorLoop(
        broker,
        BotConfig(min_signals_present=2, require_trend_alignment=False, allow_fallback=False),
        snapshot_provider=lambda: [snapshot],
        confirmation=lambda symbol: rich,
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    first = loop.run_once()
    assert not first.entries  # first pass arms the double-confirmation streak

    summary = loop.run_once(first)

    assert summary.entries[0].symbol == "UP"
