from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from god_help_tradebot.schedule import EASTERN, equity_order_session
from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker, Quote
from god_help_tradebot.monitor import ExitPlan, LoopSummary
from god_help_tradebot.paper import OrderSide, OrderType, OrderStatus, PaperOrder, Position
from god_help_tradebot.state import EquityPerformanceStore, StateStore
from god_help_tradebot.orderflow import orderflow_score, full_day_extremes


@pytest.mark.parametrize("timestamp, expected", [
    ("2026-11-24T00:00", "NIGHT"),
    ("2026-11-24T04:00", "ALL"),
    ("2026-11-24T09:30", "CORE"),
    ("2026-11-24T16:00", "ALL"),
    ("2026-11-24T20:00", "NIGHT"),
    ("2026-11-24T23:59", "NIGHT"),
    ("2026-11-27T12:59", "CORE"),
    ("2026-11-27T13:00", "ALL"),
    ("2026-11-27T20:00", "CLOSED"),
    ("2026-11-28T00:00", "CLOSED"),
    ("2026-11-29T20:00", "NIGHT"),
])
def test_full_day_session_boundaries(timestamp, expected):
    assert equity_order_session(datetime.fromisoformat(timestamp).replace(tzinfo=EASTERN)) == expected


@pytest.mark.parametrize("timestamp", ["2026-11-24T00:00", "2026-11-24T20:00", "2026-11-24T23:59", "2026-11-29T20:00"])
def test_scheduler_overnight_session_builds_valid_broker_limit_payload(timestamp):
    from god_help_tradebot.broker import WebullSandboxBroker

    session = equity_order_session(datetime.fromisoformat(timestamp).replace(tzinfo=EASTERN))
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    payload = broker._build_order_payload("TEST", OrderSide.BUY, 1, OrderType.LIMIT,
                                          limit_price=100, trading_session=session)
    assert payload["support_trading_session"] == "NIGHT"
    assert payload["order_type"] == "LIMIT"


def test_allocation_quantity_caps_orders_and_buys_exact_remainder():
    from god_help_tradebot.risk import allocation_quantity

    assert allocation_quantity(Decimal("500000"), Decimal("1"), Decimal("490000")) == 199999
    assert allocation_quantity(Decimal("2000"), Decimal("1"), Decimal("1245")) == 1245
    assert allocation_quantity(Decimal("2000"), Decimal("3"), Decimal("2.99")) == 0
    with pytest.raises(ValueError):
        allocation_quantity(Decimal("NaN"), Decimal("1"), Decimal("100"))


def test_full_allocation_submits_two_caps_then_exact_remainder(tmp_path):
    from god_help_tradebot.screen import MarketSnapshot, RankedCandidate, ProfiledCandidate

    target = Decimal(199999 * 2 + 1245)
    broker = PaperBroker(target / Decimal("0.98"))
    broker.get_buying_power = lambda: broker.cash
    quote = Quote("TEST", Decimal("1"), Decimal("1"), Decimal("1"))
    broker.last_quotes["TEST"] = quote
    ledger = EquityPerformanceStore(tmp_path / "ledger.json", "account")
    config = BotConfig(daily_peak_mode=True, account_price_priority=True, full_account_allocation=True)
    loop = MonitorLoop(broker, config, now_provider=_now, performance_store=ledger, allow_entries=True,
                       snapshot_provider=lambda: [])
    snapshot = MarketSnapshot("TEST", 1, 2, 100000, 100000, 1, 1, 1, 1)
    summary = LoopSummary()
    loop._enter((ProfiledCandidate(RankedCandidate(snapshot, 0.9), "momentum"), False), summary)
    assert summary.entries[0].quantity == 199999
    loop.run_once(summary)
    assert len(summary.entries) == 1
    broker.process_quote(quote)
    loop.run_once(summary)
    assert [order.quantity for order in summary.entries] == [199999, 199999]
    broker.process_quote(quote)
    loop.run_once(summary)
    assert [order.quantity for order in summary.entries] == [199999, 199999, 1245]
    broker.process_quote(quote)
    loop.run_once(summary)
    assert ledger.data["allocation"] is None
    assert ledger.data["active"]["TEST"]["entry_quantity"] == int(target)
    assert ledger.data["trades"] == []
    assert loop._entry_fills["TEST"] == Decimal("1")
    assert loop.exit_plans["TEST"].quantity == int(target)
    loop._now = lambda: datetime(2026, 11, 24, 23, 59, tzinfo=EASTERN)
    for _ in range(4):
        loop.run_once(summary)
        broker.process_quote(quote)
    loop.run_once(summary)
    assert [order.quantity for order in summary.exits] == [199999, 199999, 1245]
    assert len(ledger.data["trades"]) == 1
    assert ledger.data["trades"][0]["entry_quantity"] == int(target)
    assert not ledger.data["active"]


def test_allocation_resumes_after_priced_fill_without_tracked_order(tmp_path):
    from god_help_tradebot.screen import MarketSnapshot, RankedCandidate, ProfiledCandidate

    broker = PaperBroker(500000)
    broker.get_buying_power = lambda: broker.cash
    quote = Quote("TEST", Decimal("1"), Decimal("1"), Decimal("1"))
    broker.last_quotes["TEST"] = quote
    ledger = EquityPerformanceStore(tmp_path / "ledger.json", "account")
    config = BotConfig(daily_peak_mode=True, account_price_priority=True, full_account_allocation=True)
    loop = MonitorLoop(broker, config, now_provider=_now, performance_store=ledger, allow_entries=True)
    snapshot = MarketSnapshot("TEST", 1, 2, 100000, 100000, 1, 1, 1, 1)
    summary = LoopSummary()
    loop._enter((ProfiledCandidate(RankedCandidate(snapshot, 0.9), "momentum"), False), summary)
    broker.process_quote(quote)
    loop._log_fills(summary)
    restored = MonitorLoop(broker, config, now_provider=_now, performance_store=EquityPerformanceStore(ledger.path, "account"),
                           allow_entries=True, snapshot_provider=lambda: [])
    restored.run_once(summary)
    assert [order.quantity for order in summary.entries] == [199999, 199999]


def _now():
    return datetime(2026, 11, 24, 11, tzinfo=EASTERN)


@pytest.mark.parametrize("short", [False, True])
def test_peak_exit_ignores_brackets_and_requires_profitable_confirmed_reversal(tmp_path, short):
    now = [_now()]
    broker = PaperBroker(1000)
    position = Position(-10 if short else 10, Decimal("100"))
    broker.positions["TEST"] = position
    plan = ExitPlan("TEST", 10, Decimal("70" if short else "130"), Decimal("110" if short else "90"),
                    "short" if short else "long", opened_day="2026-11-24")
    store = StateStore(tmp_path / "state.json")
    loop = MonitorLoop(broker, BotConfig(daily_peak_mode=True), exit_plans={"TEST": plan},
                       now_provider=lambda: now[0], state_store=store,
                       confirmation=lambda symbol: SimpleNamespace(passed=True, signals_present=3))
    summary = LoopSummary()
    for price in (70, 71, 72) if short else (130, 129, 128):
        quote = Quote("TEST", Decimal(price), Decimal(price), Decimal(price))
        broker.last_quotes["TEST"] = quote
        loop._maybe_exit("TEST", position, summary)
        now[0] += timedelta(seconds=5)
    assert len(summary.exits) == 1
    assert summary.exits[0].side is (OrderSide.BUY if short else OrderSide.SELL)
    assert summary.exits[0].order_type is OrderType.MARKET
    assert any(event.kind == "peak_exit_order" for event in summary.events)
    assert not any(event.kind in ("stop_loss_order", "take_profit_order") for event in summary.events)
    assert store.load()["TEST"].favorable_extreme == Decimal("70" if short else "130")


def test_loss_is_held_until_deadline_then_limit_exit_intent_survives_restart(tmp_path):
    broker = PaperBroker(1000)
    broker.positions["TEST"] = Position(10, Decimal("100"))
    quote = Quote("TEST", Decimal("80"), Decimal("80.1"), Decimal("80"))
    broker.last_quotes["TEST"] = quote
    now = [_now()]
    store = StateStore(tmp_path / "state.json")
    loop = MonitorLoop(broker, BotConfig(daily_peak_mode=True),
                       exit_plans={"TEST": ExitPlan("TEST", 10, Decimal("130"), Decimal("90"), opened_day="2026-11-24")},
                       now_provider=lambda: now[0], state_store=store,
                       confirmation=lambda symbol: SimpleNamespace(passed=True, signals_present=3))
    summary = LoopSummary()
    loop._maybe_exit("TEST", broker.positions["TEST"], summary)
    assert not summary.exits
    now[0] = datetime(2026, 11, 24, 23, 59, tzinfo=EASTERN)
    loop._maybe_flatten(broker.positions, summary)
    assert summary.exits[0].order_type is OrderType.LIMIT
    assert summary.exits[0].limit_price == Decimal("79.6")
    assert store.load()["TEST"].liquidation_requested
    now[0] += timedelta(minutes=1)
    restored = MonitorLoop(broker, BotConfig(daily_peak_mode=True), state_store=store, now_provider=lambda: now[0])
    assert restored._at_flatten_time()


@pytest.mark.parametrize("day, deadline", [
    ("2026-11-24", "23:59"),  # Tuesday; overnight continues into Wednesday
    ("2026-11-25", "19:59"),  # Thanksgiving eve; no overnight into the holiday
    ("2026-11-27", "16:59"),  # early close; late trading ends at 5 p.m.
    ("2026-12-24", "16:59"),  # early close before Christmas
    ("2026-11-20", "19:59"),  # Friday
    ("2026-11-22", "23:59"),  # Sunday overnight into Monday
])
def test_daily_peak_liquidation_deadline_follows_exchange_calendar(day, deadline):
    loop = MonitorLoop(PaperBroker(1000), BotConfig(daily_peak_mode=True), flatten_before_close=True)
    cutoff = datetime.fromisoformat(f"{day}T{deadline}").replace(tzinfo=EASTERN)
    loop._now = lambda: cutoff - timedelta(minutes=1)
    assert not loop._at_flatten_time()
    loop._now = lambda: cutoff
    assert loop._at_flatten_time()


def test_early_close_after_hours_ends_at_5pm():
    assert equity_order_session(datetime(2026, 11, 27, 16, 59, tzinfo=EASTERN)) == "ALL"
    assert equity_order_session(datetime(2026, 11, 27, 17, 0, tzinfo=EASTERN)) == "CLOSED"


def _close(ledger, number, profit, *, fallback=False):
    entry = PaperOrder(number * 2, "TEST", OrderSide.BUY, 1, OrderType.MARKET,
                       status=OrderStatus.FILLED, fill_price=Decimal("100"))
    ledger.track_order(entry, "ENTRY", fallback=fallback)
    ledger.record_fill(entry, "ENTRY", _now())
    exit_order = PaperOrder(number * 2 + 1, "TEST", OrderSide.SELL, 1, OrderType.MARKET,
                            status=OrderStatus.FILLED, fill_price=Decimal(100 + profit))
    ledger.record_fill(exit_order, "EXIT", _now())
    return ledger.finalize({}, _now(), BotConfig(daily_peak_mode=True), 0.5)[0][0]


def test_each_loss_tightens_and_persists_entry_gates_across_daily_reset(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = EquityPerformanceStore(path, "account")
    _close(ledger, 1, -1)
    assert ledger.data["loss_adaptive"]["min_relative_volume"] == 2.25
    assert ledger.data["loss_adaptive"]["min_signals_present"] == 3
    assert ledger.data["loss_adaptive"]["max_spread_percent"] == 0.9
    assert ledger.data["loss_adaptive"]["min_confirmation_score"] == 0.53
    _close(ledger, 2, -1)
    assert ledger.data["loss_adaptive"]["min_relative_volume"] == 2.5

    ledger.start_daily_reviews(_now() + timedelta(days=1))
    restored = EquityPerformanceStore(path, "account")
    assert restored.data["loss_adaptive"]["min_relative_volume"] == 2.5
    assert restored.data["loss_adaptive"]["min_signals_present"] == 4
    assert restored.data["loss_adaptive"]["max_spread_percent"] == 0.8
    assert restored.data["loss_adaptive"]["min_confirmation_score"] == 0.56
    assert len(restored.data["loss_refinement_history"]) == 2


def test_review_reset_preserves_execution_history_and_reserves_half_new_profits(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "ledger.json", "account")
    old = _close(ledger, 1, 10)
    ledger.data["adaptive"] = {"min_relative_volume": 3}
    ledger.save()
    ledger.start_daily_reviews(_now())
    assert ledger.data["trades"] == [old]
    assert ledger.data["adaptive"] == {}
    assert ledger.data["review_archives"][0]["adaptive"] == {"min_relative_volume": 3}
    first = _close(ledger, 2, 20, fallback=True)
    assert first["daily_peak"]
    assert ledger.data["profit_reserve_usd"] == "10.00"
    _close(ledger, 3, -5)
    ledger = EquityPerformanceStore(ledger.path, "account")
    ledger.start_daily_reviews(_now())
    assert len(ledger.data["review_archives"]) == 1
    assert ledger.data["profit_reserve_usd"] == "10.00"
    broker = PaperBroker(1000)
    broker.get_buying_power = lambda: Decimal("1000")
    loop = MonitorLoop(broker, BotConfig(daily_peak_mode=True), performance_store=ledger)
    assert loop._account_value() == 990
    broker.get_buying_power = lambda: Decimal("5")
    assert loop._account_value() == 0


def test_main_and_fallback_peak_batches_grade_after_day_once_and_persist(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "ledger.json", "account")
    ledger.start_daily_reviews(_now())
    next_day = _now() + timedelta(days=1)
    for number in range(1, 11):
        fallback = number > 5
        trade = _close(ledger, number, 10, fallback=fallback)
        assert ledger.grade_peak(trade, _now(), Decimal("110"), Decimal("90"), 1) == []
        high = Decimal("120" if fallback or number == 5 else "110")
        reviews = ledger.grade_peak(trade, next_day, high, Decimal("90"), 1)
        if number in (5, 10):
            assert reviews[0]["wins"] == (0 if fallback else 4)
            assert reviews[0]["strategy"] == ("fallback" if fallback else "main")
        assert ledger.grade_peak(trade, next_day, high, Decimal("90"), 1) == []
    ledger = EquityPerformanceStore(ledger.path, "account")
    assert len(ledger.data["reviews"]) == 2
    assert ledger.data["fallback_reviews"] == []
    assert ledger.peak_tightening_level("main") == 0
    assert ledger.fallback_tightening_level == 1


def test_orderflow_uses_only_recent_known_side_prints_and_full_day_request():
    now = _now()
    class Response:
        status_code = 200
        def __init__(self, payload):
            self.payload = payload
        def json(self):
            return self.payload
    rows = [
        {"time": now.isoformat(), "side": "BUY", "volume": 30},
        {"time": now.isoformat(), "side": "SELL", "volume": 10},
        {"time": (now - timedelta(minutes=2)).isoformat(), "side": "SELL", "volume": 999},
        {"time": now.isoformat(), "side": "UNKNOWN", "volume": 999},
    ]
    calls = []
    start = now.replace(hour=0, minute=0)
    day_bars = [{"time": (start + timedelta(minutes=5 * index)).isoformat(), "high": 120, "low": 90, "volume": 1}
                for index in range(288)]
    market = SimpleNamespace(get_tick=lambda *args, **kwargs: Response({"result": rows}),
                             get_batch_history_bar=lambda *args, **kwargs: (
                                 calls.append(kwargs) or Response({"result": [{"result": day_bars}]})))
    broker = SimpleNamespace(session=SimpleNamespace(data_client=SimpleNamespace(market_data=market)))
    assert orderflow_score(broker, "TEST", now) == 0.75
    assert full_day_extremes(broker, "TEST", now.date()) == (Decimal("120.0"), Decimal("90.0"))
    assert len(calls[0]["trading_sessions"]) == 4
    assert "start_time" not in calls[0]
    day_bars.pop()
    assert full_day_extremes(broker, "TEST", now.date()) == (Decimal("120.0"), Decimal("90.0"))


def test_sparse_full_day_bars_can_be_graded_when_query_covers_day_boundary():
    from datetime import time

    day = _now().date()
    start = datetime.combine(day, time(), EASTERN)
    rows = [dict(time=(start + timedelta(hours=11)).isoformat(), open=100, high=120, low=90, close=110, volume=10),
            dict(time=(start - timedelta(minutes=5)).isoformat(), open=80, high=85, low=75, close=80, volume=10)]
    market = SimpleNamespace(get_batch_history_bar=lambda *args, **kwargs:
                             SimpleNamespace(status_code=200, json=lambda: {"result": [{"symbol": "TEST", "result": rows}]}))
    broker = SimpleNamespace(session=SimpleNamespace(data_client=SimpleNamespace(market_data=market)))
    assert full_day_extremes(broker, "TEST", day) == (Decimal("120"), Decimal("90"))


def test_full_day_review_rejects_endpoint_that_ignores_backward_cursor():
    row = dict(time=_now().isoformat(), high=120, low=90, volume=10)
    market = SimpleNamespace(get_batch_history_bar=lambda *args, **kwargs:
                             SimpleNamespace(status_code=200, json=lambda: {"result": [{"result": [row]}]}))
    broker = SimpleNamespace(session=SimpleNamespace(data_client=SimpleNamespace(market_data=market)))
    assert full_day_extremes(broker, "TEST", _now().date()) is None


def test_overnight_quotes_require_matching_symbol_and_fresh_timestamp(monkeypatch):
    from god_help_tradebot.broker import WebullSandboxBroker
    import god_help_tradebot.schedule as schedule

    monkeypatch.setattr(schedule, "equity_order_session", lambda now: "NIGHT")
    payload = {"basic": {"symbol": "TEST", "timestamp": datetime.now(EASTERN).isoformat()},
               "bids": [{"price": "99", "size": "10"}], "asks": [{"price": "100", "size": "20"}]}
    calls = []
    market = SimpleNamespace(get_quotes=lambda *args, **kwargs: (
        calls.append(kwargs) or SimpleNamespace(status_code=200, json=lambda: {"data": payload})))
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    broker.session = SimpleNamespace(data_client=SimpleNamespace(market_data=market))
    assert broker._get_session_quote("TEST").ask == Decimal("100")
    assert calls[0]["overnight_required"] is True
    payload["basic"]["timestamp"] = (datetime.now(EASTERN) - timedelta(minutes=1)).isoformat()
    assert broker._get_session_quote("TEST") is None
    payload["basic"]["timestamp"] = datetime.now(EASTERN).isoformat()
    payload["basic"]["symbol"] = "OTHER"
    assert broker._get_session_quote("TEST") is None


def test_night_discovery_uses_overnight_quote_fields(monkeypatch):
    from god_help_tradebot.autoscreen import enrich_with_quotes
    from god_help_tradebot.screen import MarketSnapshot
    import god_help_tradebot.schedule as schedule

    monkeypatch.setattr(schedule, "equity_order_session", lambda now: "NIGHT")
    quote = Quote("TEST", Decimal("104"), Decimal("106"), Decimal("100"),
                  overnight_price=Decimal("105"), overnight_change_ratio=Decimal("5"),
                  overnight_volume=Decimal("123"), extended_price=Decimal("99"))
    broker = SimpleNamespace(daily_peak_mode=True, get_latest_quotes=lambda symbols: {"TEST": quote})
    snapshot = MarketSnapshot(symbol="TEST", price=100, change_percent=1, volume=1000,
                              average_volume=1000, bid=99, ask=101, day_low=90, day_high=110)
    enriched, = enrich_with_quotes(broker, [snapshot])
    assert enriched.price == 105
    assert enriched.change_percent == 5
    assert enriched.volume == 123


def test_limit_chase_never_duplicates_cancel_raced_fill(monkeypatch):
    broker = PaperBroker(1000)
    broker.get_buying_power = lambda: Decimal("1000")
    broker.last_quotes["TEST"] = Quote("TEST", Decimal("99"), Decimal("100"), Decimal("100"))
    now = _now().replace(hour=5)
    loop = MonitorLoop(broker, BotConfig(daily_peak_mode=True), now_provider=lambda: now)
    order = broker.submit_order("TEST", OrderSide.BUY, 2, OrderType.LIMIT, limit_price=100.5)
    summary = LoopSummary()
    monkeypatch.setattr(loop, "_cancel_confirmed", lambda *args: False)
    loop._chase_limit_entry({"anchor": Decimal("100.5")}, order, summary)
    assert not summary.entries
    assert len(broker.orders) == 1


def test_limit_chase_respects_original_cap_and_partial_quantity(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "ledger.json", "account")
    broker = PaperBroker(1000)
    broker.get_buying_power = lambda: Decimal("1000")
    broker.last_quotes["TEST"] = Quote("TEST", Decimal("99"), Decimal("100"), Decimal("100"))
    now = _now().replace(hour=5)
    loop = MonitorLoop(broker, BotConfig(daily_peak_mode=True), now_provider=lambda: now, performance_store=ledger)
    order = broker.submit_order("TEST", OrderSide.BUY, 3, OrderType.LIMIT, limit_price=100.5)
    order.filled_quantity = 1
    order.fill_price = Decimal("100")
    broker.positions["TEST"] = Position(1, Decimal("100"))
    ledger.track_order(order, "ENTRY", fallback=True)
    loop._entry_order_ids.add(order.order_id)
    summary = LoopSummary()
    loop._chase_limit_entry({"anchor": Decimal("100.5")}, order, summary)
    assert summary.entries[0].quantity == 2
    assert ledger.data["active"]["TEST"]["fallback"]
    assert ledger.data["orders"][ledger.key(summary.entries[0])]["chase_anchor"] == "100.5"
    broker.last_quotes["TEST"] = Quote("TEST", Decimal("120"), Decimal("121"), Decimal("120"))
    loop._chase_limit_entry({"anchor": Decimal("100.5")}, summary.entries[0], summary)
    assert len(summary.entries) == 1


@pytest.mark.parametrize("short", [False, True])
def test_reserve_adjusted_cap_applies_to_fixed_dollar_sizing(short):
    from god_help_tradebot.risk import build_trade_plan, build_short_plan
    builder = build_short_plan if short else build_trade_plan
    config = BotConfig(daily_peak_mode=True)
    plan = builder(10, 11 if short else 9, config, account_value=15)
    assert plan.quantity == 1
    with pytest.raises(ValueError, match="buying power"):
        builder(10, 11 if short else 9, config, account_value=None)


def test_orderflow_scores_are_mirrored_without_fabricating_missing_data(monkeypatch):
    import god_help_tradebot.confirm as confirm
    import god_help_tradebot.orderflow as flow
    monkeypatch.setattr(confirm, "capital_flow_score", lambda *args: (0.5, None, 0))
    monkeypatch.setattr(confirm, "analyst_rating_score", lambda *args: (0.5, None))
    monkeypatch.setattr(flow, "orderflow_score", lambda *args: 0.8)
    kwargs = dict(include_news=False, include_patterns=False, include_orderflow=True)
    bullish = confirm.confirm_candidate(None, "TEST", **kwargs)
    bearish = confirm.confirm_candidate_bearish(None, "TEST", **kwargs)
    assert bullish.orderflow_score == 0.8
    assert bearish.orderflow_score == pytest.approx(0.2)
    assert bullish.score > bearish.score
    assert bullish.signals_present == 1
    monkeypatch.setattr(flow, "orderflow_score", lambda *args: None)
    assert confirm.confirm_candidate(None, "TEST", **kwargs).signals_present == 0


def test_cli_enables_daily_mode_for_server_but_not_replays(monkeypatch):
    import god_help_tradebot.cli as cli
    calls = []
    monkeypatch.setattr(cli, "load_bot_config", lambda *args: BotConfig())
    monkeypatch.setattr(cli, "run_bot", lambda **kwargs: calls.append(kwargs))
    assert cli.main(["--max-polls", "1"]) == 0
    assert calls[0]["config"].daily_peak_mode
    assert calls[0]["config"].highest_score_priority
    assert calls[0]["config"].full_account_allocation
    assert calls[0]["config"].research_enabled


def test_corrupt_daily_review_state_blocks_startup(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "ledger.json", "account")
    ledger.start_daily_reviews(_now())
    ledger.data["daily_reviews"]["trade_offset"] = -1
    ledger.save()
    with pytest.raises(RuntimeError, match="reconcile"):
        EquityPerformanceStore(ledger.path, "account")


def test_short_deadline_defers_to_next_core_session_with_persisted_intent(tmp_path):
    broker = PaperBroker(1000)
    broker.positions["TEST"] = Position(-2, Decimal("100"))
    now = [datetime(2026, 11, 27, 23, 59, tzinfo=EASTERN)]
    store = StateStore(tmp_path / "state.json")
    loop = MonitorLoop(broker, BotConfig(daily_peak_mode=True), state_store=store, now_provider=lambda: now[0],
                       exit_plans={"TEST": ExitPlan("TEST", 2, Decimal("70"), Decimal("110"), "short", opened_day="2026-11-27")})
    summary = LoopSummary()
    loop._maybe_flatten(broker.positions, summary)
    assert not summary.exits
    assert store.load()["TEST"].liquidation_requested
    now[0] = datetime(2026, 11, 30, 9, 30, tzinfo=EASTERN)
    restored = MonitorLoop(broker, BotConfig(daily_peak_mode=True), state_store=store, now_provider=lambda: now[0])
    restored._maybe_flatten(broker.positions, summary)
    assert summary.exits[0].side is OrderSide.BUY
    assert summary.exits[0].order_type is OrderType.MARKET


def test_missing_overnight_quote_keeps_deadline_intent_without_loop_error(tmp_path):
    broker = PaperBroker(1000)
    broker.positions["TEST"] = Position(2, Decimal("100"))
    loop = MonitorLoop(broker, BotConfig(daily_peak_mode=True), state_store=StateStore(tmp_path / "state.json"),
                       now_provider=lambda: datetime(2026, 11, 24, 23, 59, tzinfo=EASTERN))
    summary = LoopSummary()
    loop._maybe_flatten(broker.positions, summary)
    assert not summary.exits
    assert loop.exit_plans["TEST"].liquidation_requested
    assert summary.events[0].kind == "exit_quote_wait"
    assert loop._consecutive_errors == 0


def test_review_loggers_route_only_their_own_events():
    import logging
    from io import StringIO
    from god_help_tradebot.runner import _log_events
    from god_help_tradebot.monitor import LoopEvent

    streams = {channel: StringIO() for channel in ("all", "main", "fallback")}
    loggers = {}
    for channel, stream in streams.items():
        logger = logging.Logger(channel)
        logger.addHandler(logging.StreamHandler(stream))
        loggers[channel] = logger
    summary = LoopSummary(events=[LoopEvent("trade_completed", "TEST", "profit"),
                                  LoopEvent("loss_refinement", "TEST", "tightened"),
                                  LoopEvent("main_peak_review", "account", "main"),
                                  LoopEvent("fallback_peak_result", "TEST", "fallback")])
    _log_events(logging.Logger("application"), summary, 0, loggers)
    assert "trade_completed" in streams["all"].getvalue()
    assert "loss_refinement" in streams["all"].getvalue()
    assert "main_peak_review" in streams["main"].getvalue()
    assert "fallback_peak_result" in streams["fallback"].getvalue()
    assert "fallback" not in streams["main"].getvalue()


def test_one_trade_per_eastern_day_still_reviews_close_then_resumes_next_day(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "ledger.json", "account")
    ledger.data["active"]["DONE"] = {
        "trade_id": "t1", "symbol": "DONE", "direction": "long", "opened_at": "2026-11-24T00:30:00-05:00",
        "entry_quantity": 10, "entry_notional": "100", "exit_quantity": 10, "exit_notional": "90", "daily_peak": True,
    }
    clock = {"now": datetime(2026, 11, 24, 23, tzinfo=EASTERN)}
    scans = []
    loop = MonitorLoop(PaperBroker(100000), BotConfig(daily_peak_mode=True), now_provider=lambda: clock["now"],
                       performance_store=ledger, allow_entries=True, snapshot_provider=lambda: scans.append(clock["now"]) or [])
    summary = LoopSummary()
    loop.run_once(summary)
    loop.run_once(summary)
    kinds = [event.kind for event in summary.events]
    assert kinds.count("trade_completed") == 1 and kinds.count("loss_refinement") == 1
    assert kinds.count("daily_trade_limit") == 1
    assert scans == []
    clock["now"] = datetime(2026, 11, 25, 0, 1, tzinfo=EASTERN)
    loop.run_once(summary)
    assert scans == [clock["now"]]