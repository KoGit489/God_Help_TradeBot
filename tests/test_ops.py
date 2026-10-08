import json
from datetime import datetime
from decimal import Decimal

from god_help_tradebot import BotConfig, ExitPlan, MonitorLoop, PaperBroker, Position, Quote, StateStore, load_bot_config
from god_help_tradebot.schedule import EASTERN


# --- StateStore (persistence) ---------------------------------------------

def test_state_store_roundtrip(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    plans = {
        "NIVF": ExitPlan("NIVF", 100, Decimal("0.15"), Decimal("0.09")),
    }
    store.save(plans)

    loaded = store.load()

    assert loaded["NIVF"].quantity == 100
    assert loaded["NIVF"].target_price == Decimal("0.15")
    assert loaded["NIVF"].stop_price == Decimal("0.09")


def test_state_store_missing_file_returns_empty(tmp_path) -> None:
    assert StateStore(tmp_path / "nope.json").load() == {}


def test_state_store_corrupt_file_returns_empty(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{ not json", encoding="utf-8")
    assert StateStore(path).load() == {}


def test_monitor_restores_persisted_plans(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    store.save({"NIVF": ExitPlan("NIVF", 100, Decimal("0.15"), Decimal("0.09"))})

    loop = MonitorLoop(PaperBroker(10_000), state_store=store)

    assert "NIVF" in loop.exit_plans


def test_monitor_persists_plan_after_entry(tmp_path) -> None:
    from god_help_tradebot.screen import MarketSnapshot

    store = StateStore(tmp_path / "state.json")
    snapshot = MarketSnapshot("GOW", 3.0, 5.0, 500_000, 100_000, 2.99, 3.01, 2.80, 3.20)
    loop = MonitorLoop(
        PaperBroker(10_000),
        snapshot_provider=lambda: [snapshot],
        now_provider=lambda: datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN),
        allow_entries=True,
        state_store=store,
        flatten_before_close=False,
    )

    loop.run_once()

    assert "GOW" in store.load()


# --- Resilience ------------------------------------------------------------

class _FlakyBroker(PaperBroker):
    def __init__(self, fail_times: int, starting_cash: float = 10_000) -> None:
        super().__init__(starting_cash)
        self._fail_times = fail_times
        self.calls = 0

    def get_positions(self):
        self.calls += 1
        if self.calls <= self._fail_times:
            raise ConnectionError("transient API blip")
        return super().get_positions()


def test_monitor_survives_transient_errors() -> None:
    broker = _FlakyBroker(fail_times=2)
    loop = MonitorLoop(broker, poll_seconds=0, max_polls=3, flatten_before_close=False, error_backoff_seconds=0)

    summary = loop.run()

    errors = [e for e in summary.events if e.kind == "error"]
    assert len(errors) == 2
    # The loop recovered from the transient blips and kept polling past them.
    assert not any(e.kind == "halt" for e in summary.events)
    assert summary.polls >= 3


def test_monitor_halts_after_max_consecutive_errors() -> None:
    broker = _FlakyBroker(fail_times=99)
    loop = MonitorLoop(broker, poll_seconds=0, max_consecutive_errors=3, flatten_before_close=False, error_backoff_seconds=0)

    summary = loop.run()

    assert any(e.kind == "halt" for e in summary.events)


# --- End-of-day flatten ------------------------------------------------------

def _near_close_now() -> datetime:
    # 3:58 PM ET is past the 3-minute-before-close exit cutoff.
    return datetime(2026, 10, 7, 15, 58, tzinfo=EASTERN)


def test_monitor_flattens_positions_before_close() -> None:
    broker = PaperBroker(10_000)
    broker.positions["NIVF"] = Position(quantity=100, average_price=Decimal("0.10"))
    broker.last_quotes["NIVF"] = Quote("NIVF", Decimal("0.11"), Decimal("0.12"), Decimal("0.11"))
    loop = MonitorLoop(
        broker,
        exit_plans={"NIVF": ExitPlan("NIVF", 100, Decimal("0.50"), Decimal("0.05"))},
        now_provider=_near_close_now,
        flatten_before_close=True,
    )

    summary = loop.run_once()

    assert summary.exits[0].side.value == "SELL"
    assert any(e.kind == "eod_flatten" for e in summary.events)
    assert "NIVF" not in loop.exit_plans


def test_monitor_does_not_flatten_midday() -> None:
    broker = PaperBroker(10_000)
    broker.positions["NIVF"] = Position(quantity=100, average_price=Decimal("0.10"))
    broker.last_quotes["NIVF"] = Quote("NIVF", Decimal("0.11"), Decimal("0.12"), Decimal("0.11"))
    loop = MonitorLoop(
        broker,
        exit_plans={"NIVF": ExitPlan("NIVF", 100, Decimal("0.50"), Decimal("0.05"))},
        now_provider=lambda: datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN),
        flatten_before_close=True,
    )

    summary = loop.run_once()

    assert not any(e.kind == "eod_flatten" for e in summary.events)


# --- Exit blocking on deterministic rejections -------------------------------

class _RejectedBroker(PaperBroker):
    """Raises a 417-style rejection on every sell, like an ineligible symbol."""

    def submit_order(self, symbol, side, quantity, order_type, **kwargs):
        if side.value == "SELL":
            raise RuntimeError("HTTP Status: 417, Code: OPENAPI_OVERNIGHT_CANT_SUPPORT_TICKER")
        return super().submit_order(symbol, side, quantity, order_type, **kwargs)


def test_monitor_blocks_rejected_exit_without_halting() -> None:
    broker = _RejectedBroker(10_000)
    broker.positions["NIVF"] = Position(quantity=100, average_price=Decimal("0.10"))
    broker.last_quotes["NIVF"] = Quote("NIVF", Decimal("0.08"), Decimal("0.09"), Decimal("0.08"))
    loop = MonitorLoop(
        broker,
        exit_plans={"NIVF": ExitPlan("NIVF", 100, Decimal("0.50"), Decimal("0.09"))},
        now_provider=_open_session_now,
        flatten_before_close=False,
        error_backoff_seconds=0,
    )

    summary = loop.run_once()
    assert any(e.kind == "exit_blocked" for e in summary.events)

    # Second poll in the same session: skips the rejected exit, no halt.
    summary2 = loop.run_once()
    assert not any(e.kind == "error" for e in summary2.events)
    assert "NIVF" in loop._exit_blocked


def _open_session_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


# --- Config file -------------------------------------------------------------

def test_load_bot_config_defaults_when_no_file(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    config = load_bot_config(tmp_path / "missing.json")
    assert config.risk_per_trade_usd == BotConfig().risk_per_trade_usd


def test_load_bot_config_applies_json_overrides(tmp_path) -> None:
    path = tmp_path / "bot_config.json"
    path.write_text(json.dumps({"risk_per_trade_usd": 50.0, "max_symbol_price": 5.0}), encoding="utf-8")

    config = load_bot_config(path)

    assert config.risk_per_trade_usd == 50.0
    assert config.max_symbol_price == 5.0


def test_load_bot_config_env_overrides_file(tmp_path, monkeypatch) -> None:
    path = tmp_path / "bot_config.json"
    path.write_text(json.dumps({"risk_per_trade_usd": 50.0}), encoding="utf-8")
    monkeypatch.setenv("BOT_RISK_PER_TRADE_USD", "10.0")

    config = load_bot_config(path)

    assert config.risk_per_trade_usd == 10.0


def test_load_bot_config_ignores_malformed(tmp_path) -> None:
    path = tmp_path / "bot_config.json"
    path.write_text("{ broken", encoding="utf-8")

    config = load_bot_config(path)

    assert config == BotConfig()
