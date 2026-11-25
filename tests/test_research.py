from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from god_help_tradebot import BotConfig
from god_help_tradebot.backtest import ExecutionStress, ResearchBroker, run_research_backtest
from god_help_tradebot.paper import OrderSide, OrderType, Quote, OrderStatus
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.research import BOUNDS, POLICY, ResearchPipeline, ExecutionController, acceptable, fixed_fingerprint, valid_promotion, write_json
from god_help_tradebot import MonitorLoop, PaperBroker
from god_help_tradebot.monitor import LoopSummary
from god_help_tradebot.state import EquityPerformanceStore


def sample_day():
    now = datetime(2026, 11, 24, 11, tzinfo=EASTERN)
    snapshot = dict(symbol="TEST", price=10, change_percent=3, volume=300000, average_volume=100000,
                    bid=10, ask=10, day_low=9, day_high=11)
    report = dict(score=0.9, passed=True, signals_present=3)
    steps = []
    for index, price in enumerate((10, 10, 10, 13, 12.9, 12.8, 12.7, 12.6)):
        steps.append(dict(time=(now + timedelta(seconds=index * 5)).isoformat(), snapshots=[snapshot],
                          quotes=[dict(symbol="TEST", bid=price, ask=price, last=price, bid_volume=100000, ask_volume=100000)],
                          confirmations={"TEST": {"bullish": report, "bearish": report}}))
    steps.append(dict(time=now.replace(hour=23, minute=59).isoformat(), snapshots=[],
                      quotes=[dict(symbol="TEST", bid=12.6, ask=12.6, last=12.6, bid_volume=100000, ask_volume=100000)]))
    steps.append(dict(time=now.replace(hour=23, minute=59, second=10).isoformat(), snapshots=[],
                      quotes=[dict(symbol="TEST", bid=12.6, ask=12.6, last=12.6, bid_volume=100000, ask_volume=100000)]))
    return steps


def test_research_replays_real_monitor_without_account_calls(tmp_path):
    config = BotConfig(daily_peak_mode=True, full_account_allocation=True, highest_score_priority=True,
                       allow_fallback=False)
    result = run_research_backtest(sample_day(), config, tmp_path / "isolated", starting_cash=1000)
    assert result["trades"] >= 1
    assert result["fees"] > 0
    assert result["net_pnl"] < result["gross_pnl"]
    assert result["errors"] == 0
    assert not result["open_exposure"]
    assert not (tmp_path / "equity_performance.json").exists()


def test_missing_historical_signals_are_not_invented(tmp_path):
    timeline = sample_day()
    for step in timeline:
        step.pop("confirmations", None)
    result = run_research_backtest(timeline, BotConfig(daily_peak_mode=True, allow_fallback=False), tmp_path / "isolated")
    assert result["trades"] == 0
    assert result["missing_signals"] > 0


def test_research_requires_strict_chronology(tmp_path):
    with pytest.raises(ValueError, match="chronological"):
        run_research_backtest(list(reversed(sample_day())), BotConfig(), tmp_path / "isolated")


def test_research_partial_fills_respect_depth_and_limit():
    broker = ResearchBroker(1000, ExecutionStress(partial_fraction=0.5, slippage_bps=0))
    broker.now = datetime(2026, 11, 24, 11, tzinfo=EASTERN)
    order = broker.submit_order("TEST", OrderSide.BUY, 10, OrderType.LIMIT, limit_price=10)
    broker.process_quote(Quote("TEST", Decimal("10"), Decimal("10"), Decimal("10"), ask_volume=Decimal("3")))
    assert order.filled_quantity == 3
    assert order.status is OrderStatus.OPEN
    broker.process_quote(Quote("TEST", Decimal("11"), Decimal("11"), Decimal("11"), ask_volume=Decimal("100")))
    assert order.filled_quantity == 3


def metrics(profit=100, trades=100):
    return dict(trades=trades, net_pnl=profit, drawdown=0.05, closed_drawdown=0.05, loss_severity=0.01,
                open_exposure=False, errors=0, trade_keys=[str(index) for index in range(trades)])


def artifact(config):
    import hashlib
    import json

    parameters = {key: getattr(config, key) for key in BOUNDS}
    parameters["entry_chase_polls"] = 1
    fingerprint = "verified-dataset"
    version = hashlib.sha256(json.dumps(parameters, sort_keys=True).encode() + fingerprint.encode()).hexdigest()[:16]
    pair = {"baseline": metrics(100), "candidate": metrics(110)}
    return {"version": version, "parameters": parameters, "previous": {key: getattr(config, key) for key in BOUNDS},
            "policy": POLICY, "fixed_fingerprint": fixed_fingerprint(config), "training": pair,
            "evidence": {"audit": {"ready": True, "trading_days": 1200, "fingerprint": fingerprint},
                         "walk_forward": [pair] * 3, "holdout": pair, "stress": [pair] * 3,
                         "shadow": {**pair, "paired_trades": 100}}}


def test_promotion_requires_evidence_and_exact_approved_bounds():
    promotion = artifact(BotConfig())
    assert valid_promotion(promotion)
    promotion["parameters"]["entry_chase_polls"] = 5
    assert not valid_promotion(promotion)
    assert not acceptable(metrics(), metrics(110, trades=4), 30)
    assert not acceptable(metrics(), {**metrics(110), "open_exposure": True}, 30)
    assert not acceptable(metrics(), {**metrics(110), "drawdown": 0.06}, 30)


def test_controller_promotes_only_when_flat_and_rolls_back_persistently(tmp_path):
    from god_help_tradebot.paper import Position

    config = BotConfig(daily_peak_mode=True)
    ledger = EquityPerformanceStore(tmp_path / "account.json", "account")
    broker = PaperBroker(1000)
    loop = MonitorLoop(broker, config, performance_store=ledger)
    root = tmp_path / "research"
    write_json(root / "policy.json", POLICY)
    promotion = artifact(config)
    write_json(root / "promotion.json", promotion)
    controller = ExecutionController(root)
    summary = LoopSummary()
    broker.positions["OTHER"] = Position(1, Decimal("10"))
    controller.tick(loop, summary)
    assert ledger.data["execution_refinements"]["active"] is None
    broker.positions.clear()
    controller.tick(loop, summary)
    assert loop.config.entry_chase_polls == 1
    assert loop.config.stop_pct == config.stop_pct
    assert loop.config.full_account_allocation == config.full_account_allocation
    restored = EquityPerformanceStore(ledger.path, "account")
    assert restored.data["execution_refinements"]["active"]["version"] == promotion["version"]
    ledger.data["trades"] = [dict(realized_pnl_usd="-1", entry_quantity=1, exit_quantity=1, entry_notional="100") for _ in range(30)]
    controller.tick(loop, summary)
    assert loop.config.entry_chase_polls == 2
    assert ledger.data["execution_refinements"]["active"] is None
    assert promotion["version"] in ledger.data["execution_refinements"]["rejected"]
    assert (root / "rollback.json").is_file()
    controller.tick(loop, summary)
    assert loop.config.entry_chase_polls == 2


def test_audit_blocks_missing_history_and_never_calls_evaluator(tmp_path):
    pipeline = ResearchPipeline(tmp_path / "research", tmp_path / "data", evaluator=lambda *args: pytest.fail("must not replay missing data"))
    pipeline.initialize(BotConfig())
    result = pipeline.run()
    assert result["status"] == "blocked_data"
    assert "five-year trading-day coverage unavailable" in result["audit"]["reasons"]
    assert not (pipeline.root / "promotion.json").exists()


def test_sequential_pipeline_reaches_shadow_only_after_all_historical_gates(tmp_path, monkeypatch):
    import god_help_tradebot.research as research

    now = datetime(2026, 11, 24, tzinfo=EASTERN)
    start = now - timedelta(days=1900)
    steps = [dict(time=(start + timedelta(days=index)).isoformat()) for index in range(1900)]
    audit = dict(ready=True, fingerprint="dataset", dataset_id="generation-1", trading_days=1200,
                 last_day="2026-11-23")
    monkeypatch.setattr(research, "audit_dataset", lambda *args: (audit, steps))
    calls = []
    def evaluate(timeline, config, directory, stress):
        calls.append((config.entry_chase_polls, len(timeline), stress))
        return metrics(110 if config.entry_chase_polls == 1 else 100)
    pipeline = ResearchPipeline(tmp_path / "research", tmp_path / "data", evaluator=evaluate)
    pipeline.initialize(BotConfig())
    result = pipeline.run(now)
    assert result["status"] == "awaiting_forward_shadow"
    assert len(result["walk_forward"]) == 3
    assert len(result["stress"]) == 3
    assert not (pipeline.root / "promotion.json").exists()
    first_calls = len(calls)
    pipeline.run(now)
    assert len(calls) == first_calls
    audit["ready"] = False
    audit["reasons"] = ["temporary missing file"]
    assert pipeline.run(now)["status"] == "blocked_data"
    audit["ready"] = True
    assert pipeline.run(now)["status"] == "awaiting_forward_shadow"
    assert len(calls) == first_calls


def test_confirmation_recording_preserves_only_observed_fields(tmp_path):
    from types import SimpleNamespace
    from god_help_tradebot.recorder import SessionRecorder
    from god_help_tradebot.research import merge_steps

    when = datetime(2026, 11, 24, 11, tzinfo=EASTERN)
    recorder = SessionRecorder(tmp_path)
    recorder.record_confirmation(when, "TEST", "bullish", SimpleNamespace(score=0.8, passed=True, signals_present=2, api_secret="not-recorded"))
    merged = merge_steps(list(tmp_path.glob("*.jsonl")), now=when)
    report = merged[0]["confirmations"]["TEST"]["bullish"]
    assert report["asof"] == when.isoformat()
    assert "orderflow_score" not in report
    assert "api_secret" not in report


def test_worker_filters_credentials_and_keeps_process_isolated(tmp_path, monkeypatch):
    import subprocess
    from god_help_tradebot.research import ResearchWorker

    calls = []
    class Process:
        pid = 123
        def poll(self):
            return 0
    monkeypatch.setenv("WEBULL_API_SECRET", "must-not-inherit")
    monkeypatch.setenv("ALPHAVANTAGE_API_KEY", "must-not-inherit")
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)) or Process())
    worker = ResearchWorker(tmp_path / "research", tmp_path / "data")
    worker.tick(BotConfig())
    command, options = calls[0]
    assert "god_help_tradebot.research" in command[0]
    assert "WEBULL_API_SECRET" not in options["env"]
    assert "ALPHAVANTAGE_API_KEY" not in options["env"]
    worker.tick(BotConfig())
    assert len(calls) == 1
    worker.close()


def historical_response(times, status=200):
    from types import SimpleNamespace
    bars = [dict(time=when.isoformat(), open="1", high="2", low="0.5", close="1", volume="100") for when in times]
    return SimpleNamespace(status_code=status, json=lambda: {"result": [{"symbol": "TEST", "result": bars}]})


def test_webull_history_pages_backward_resumes_and_stops_at_empty_page(tmp_path):
    from god_help_tradebot.webull_history import WebullHistoryCollector
    from god_help_tradebot.research import read_json

    now = datetime(2026, 11, 25, 12, tzinfo=EASTERN)
    calls = []
    responses = iter([historical_response([now - timedelta(days=1), now - timedelta(days=1)]),
                      historical_response([now - timedelta(days=2)]), historical_response([])])
    def get_bars(*args, **kwargs):
        calls.append(kwargs)
        return next(responses)
    collector = WebullHistoryCollector(tmp_path, get_bars)
    collector.seed(["TEST"], now)
    assert collector.tick(now)["bars"] == 1
    assert collector.tick(now)["status"] == "cooldown"
    restored = WebullHistoryCollector(tmp_path, get_bars)
    assert restored.tick(now + timedelta(minutes=2))["bars"] == 2
    assert calls[1]["end_time"] < calls[0]["end_time"]
    assert calls[0]["trading_sessions"] == ["PRE", "RTH", "ATH", "OVN"]
    assert calls[0]["count"] == 1200
    assert "start_time" not in calls[0]
    assert restored.tick(now + timedelta(minutes=4))["status"] == "exhausted"
    assert read_json(tmp_path / "coverage.json")["promotion_eligible"] is False


def test_webull_history_does_not_loop_when_api_ignores_older_cursor(tmp_path):
    from god_help_tradebot.webull_history import WebullHistoryCollector

    now = datetime(2026, 11, 25, tzinfo=EASTERN)
    response = historical_response([now - timedelta(days=1)])
    collector = WebullHistoryCollector(tmp_path, lambda *args, **kwargs: response)
    collector.seed(["TEST"], now)
    assert collector.tick(now)["status"] == "collecting"
    assert collector.tick(now + timedelta(minutes=2))["status"] == "no_progress"
    assert collector.tick(now + timedelta(minutes=4))["status"] == "idle"


@pytest.mark.parametrize("status, expected", [(403, "permission_blocked"), (429, "rate_limited")])
def test_webull_history_handles_permissions_and_backoff_without_advancing_cursor(tmp_path, status, expected):
    from god_help_tradebot.webull_history import WebullHistoryCollector
    from god_help_tradebot.research import read_json

    now = datetime(2026, 11, 25, tzinfo=EASTERN)
    collector = WebullHistoryCollector(tmp_path, lambda *args, **kwargs: historical_response([], status))
    collector.seed(["TEST"], now)
    initial = read_json(tmp_path / "collection.json")["symbols"]["TEST-M5"]["end_time"]
    assert collector.tick(now)["status"] == expected
    assert read_json(tmp_path / "collection.json")["symbols"]["TEST-M5"]["end_time"] == initial
    assert collector.tick(now + timedelta(seconds=30))["status"] == "cooldown"


def test_webull_history_targets_five_years_with_leap_day_safe_bounds(tmp_path):
    from god_help_tradebot.webull_history import WebullHistoryCollector
    from god_help_tradebot.research import read_json

    now = datetime(2028, 2, 29, tzinfo=EASTERN)
    minute = WebullHistoryCollector(tmp_path, lambda *args, **kwargs: historical_response([]))
    daily = WebullHistoryCollector(tmp_path, lambda *args, **kwargs: historical_response([]), timespan="D")
    minute.seed(["TEST"], now)
    daily.seed(["TEST"], now)
    records = read_json(tmp_path / "collection.json")["symbols"]
    assert records["TEST-M5"]["start_time"] == int(datetime(2023, 2, 28, tzinfo=EASTERN).timestamp() * 1000)
    assert records["TEST-D"]["start_time"] == int(datetime(2023, 2, 28, tzinfo=EASTERN).timestamp() * 1000)


def test_webull_history_defers_api_calls_while_managing_position(tmp_path):
    from types import SimpleNamespace
    from god_help_tradebot.webull_history import WebullHistoryResearch

    history = WebullHistoryResearch(tmp_path, lambda *args, **kwargs: pytest.fail("must defer history for positions"))
    loop = SimpleNamespace(exit_plans={"TEST": object()})
    assert history.tick(loop)["status"] == "deferred_for_trading"


def test_daily_history_uses_verified_end_only_paging(tmp_path):
    from god_help_tradebot.webull_history import WebullHistoryCollector

    now = datetime(2026, 11, 25, tzinfo=EASTERN)
    calls = []
    def get_bars(*args, **kwargs):
        calls.append(kwargs)
        return historical_response([now - timedelta(days=1)])
    collector = WebullHistoryCollector(tmp_path, get_bars, timespan="D")
    collector.seed(["TEST"], now)
    collector.tick(now)
    assert "end_time" in calls[0]
    assert "start_time" not in calls[0]
    assert "trading_sessions" not in calls[0]