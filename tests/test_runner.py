import logging
from types import SimpleNamespace

import pytest
from god_help_tradebot.monitor import LoopEvent

from god_help_tradebot import BotConfig
from god_help_tradebot import runner
from god_help_tradebot.broker import WebullSandboxBroker


def test_service_unavailable_classification_is_specific():
    from god_help_tradebot.broker import is_service_unavailable_error

    assert is_service_unavailable_error(RuntimeError("HTTP Status: 503, Code: SERVICE_NOT_AVAILABLE"))
    assert is_service_unavailable_error(SimpleNamespace(status_code=503))
    assert not is_service_unavailable_error(RuntimeError("Cannot verify server-side open orders"))
    assert not is_service_unavailable_error(RuntimeError("HTTP Status: 429, Code: TOO_MANY_REQUESTS"))


def test_startup_retries_only_service_unavailable(monkeypatch, caplog):
    sleeps = []
    attempts = []

    def account_check():
        attempts.append(1)
        if len(attempts) <= 6:
            raise RuntimeError("HTTP Status: 503, Code: SERVICE_NOT_AVAILABLE")
        return "verified"

    monkeypatch.setattr(runner.time, "sleep", sleeps.append)
    assert runner._retry_account_read(account_check, logging.getLogger("test-startup-retry"), None) == "verified"
    assert sleeps == [30, 60, 120, 240, 300, 300]
    assert "trading remains blocked" in caplog.text

    with pytest.raises(RuntimeError, match="unsafe order"):
        runner._retry_account_read(lambda: (_ for _ in ()).throw(RuntimeError("unsafe order")),
                                   logging.getLogger("test-startup-retry"), None)
    assert len(sleeps) == 6


def test_startup_retry_honors_stop_flag(monkeypatch):
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: pytest.fail("must stop before sleeping"))
    with pytest.raises(runner._StartupStopped):
        runner._retry_account_read(lambda: (_ for _ in ()).throw(RuntimeError("HTTP 503")),
                                   logging.getLogger("test-startup-stop"), lambda: True)


def test_remote_orders_503_is_retryable_but_other_responses_block():
    from god_help_tradebot.broker import is_service_unavailable_error

    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    broker._resolve_account_id = lambda: "test-account"
    response = SimpleNamespace(status_code=503)
    broker.session = SimpleNamespace(trade_client=SimpleNamespace(
        order_v3=SimpleNamespace(get_order_open=lambda *args, **kwargs: response)
    ))
    with pytest.raises(RuntimeError) as error:
        broker.assert_no_remote_open_orders()
    assert is_service_unavailable_error(error.value)


class _Broker:
    sandbox_mode = True
    live_mode = False

    def connect(self):
        return self

    def assert_no_remote_open_orders(self):
        pass

    def get_positions(self):
        return {}


def test_startup_rechecks_orders_after_positions_503(monkeypatch):
    class RecoveringBroker(_Broker):
        def __init__(self):
            self.calls = []

        def assert_no_remote_open_orders(self):
            self.calls.append("orders")

        def get_positions(self):
            self.calls.append("positions")
            if self.calls.count("positions") == 1:
                raise RuntimeError("HTTP 503")
            return {}

    broker = RecoveringBroker()
    monkeypatch.setattr(runner.WebullSandboxBroker, "from_env", lambda: broker)
    monkeypatch.setattr(runner.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(runner, "_setup_logging", lambda path: logging.getLogger("test-startup-reconcile"))
    result = runner.run_bot(config=BotConfig(), record_data=False, stop_flag=lambda: False, max_polls=0)
    assert result.polls == 0
    assert broker.calls == ["orders", "positions", "orders", "positions"]


class _Loop:
    max_consecutive_errors = 1
    error_backoff_seconds = 0

    def __init__(self, *args, **kwargs):
        self._consecutive_errors = 0

    def run_once(self, summary):
        summary.polls += 1
        self._consecutive_errors = self.max_consecutive_errors
        return summary


def test_fatal_error_halt_propagates_failure_to_systemd(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(runner.WebullSandboxBroker, "from_env", lambda: _Broker())
    monkeypatch.setattr(runner, "MonitorLoop", _Loop)
    monkeypatch.setattr(runner, "_setup_logging", lambda path: logging.getLogger("test-runner"))
    monkeypatch.setattr(runner.signal, "signal", lambda signum, handler: None)

    with pytest.raises(RuntimeError, match="halted after 1 consecutive poll errors"):
        runner.run_bot(
            config=BotConfig(),
            state_file=tmp_path / "state.json",
            log_file=tmp_path / "bot.log",
        )


@pytest.mark.parametrize("payload, allowed", [([], True), ([{"symbol": "RMSG"}], False), ({}, False), (None, False)])
def test_remote_orders_block_unsafe_startup(payload, allowed) -> None:
    response = SimpleNamespace(status_code=200, json=lambda: payload)
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    broker._resolve_account_id = lambda: "test-account"
    broker.session = SimpleNamespace(trade_client=SimpleNamespace(
        order_v3=SimpleNamespace(get_order_open=lambda *args, **kwargs: response)
    ))
    if allowed:
        broker.assert_no_remote_open_orders()
    else:
        with pytest.raises(RuntimeError, match="startup blocked"):
            broker.assert_no_remote_open_orders()


@pytest.mark.parametrize("buying_power, expected", [(1_000_000, "$1000000.00"), (None, "unavailable")])
def test_startup_reports_account_buying_power_before_polling(monkeypatch, tmp_path, caplog, buying_power, expected):
    broker = _Broker()
    calls = []

    def balance():
        calls.append("balance")
        return buying_power

    broker.get_buying_power = balance
    monkeypatch.setattr(runner.WebullSandboxBroker, "from_env", lambda: broker)
    monkeypatch.setattr(runner, "_setup_logging", lambda path: logging.getLogger("test-account-startup"))
    monkeypatch.setattr(runner.signal, "signal", lambda signum, handler: None)
    with caplog.at_level(logging.INFO):
        result = runner.run_bot(
            config=BotConfig(account_price_priority=True), state_file=tmp_path / "state.json",
            record_data=False, stop_flag=lambda: True,
        )
    assert result.polls == 0
    assert calls == ["balance"]
    assert "account_buying_power" in caplog.text
    assert expected in caplog.text


def test_continuous_review_runner_bounds_recent_events(monkeypatch, tmp_path):
    class AccountBroker(_Broker):
        @classmethod
        def from_env(cls):
            return cls()

        def _resolve_account_id(self):
            return "test-account"

        def assert_no_remote_open_orders(self, **kwargs):
            pass

    class BusyLoop(_Loop):
        def run_once(self, summary):
            summary.polls += 1
            summary.events.extend([LoopEvent("test", "TEST", "event")] * 1500)
            return summary

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "WebullSandboxBroker", AccountBroker)
    monkeypatch.setattr(runner, "MonitorLoop", BusyLoop)
    monkeypatch.setattr(runner, "_setup_logging", lambda path: logging.getLogger("test-bounded-events"))
    monkeypatch.setattr(runner, "_maybe_run_study", lambda *args: None)
    monkeypatch.setattr(runner.signal, "signal", lambda *args: None)
    monkeypatch.setattr("time.sleep", lambda *args: None)
    stop = iter([False, True])
    result = runner.run_bot(config=BotConfig(), record_data=False, stop_flag=lambda: next(stop))
    assert result.polls == 1
    assert len(result.events) == 1000


def test_actual_trade_review_disables_automatic_replay_study(monkeypatch):
    monkeypatch.setattr(runner, "run_study", lambda *args, **kwargs: pytest.fail("must not replay during actual-trade review"))
    runner._maybe_run_study(logging.getLogger("test-review"), BotConfig(trade_review_enabled=True),
                            "unused", "unused", "unused")


def test_runner_sleeps_for_rate_limit_cooldown(monkeypatch, tmp_path):
    sleeps = []

    class RateLimitedLoop(_Loop):
        max_consecutive_errors = 5
        error_backoff_seconds = 300
        _rate_limit_backoff_active = False

        def run_once(self, summary):
            summary.polls += 1
            self._consecutive_errors = 0
            self._rate_limit_backoff_active = True
            return summary

    monkeypatch.setattr(runner.WebullSandboxBroker, "from_env", lambda: _Broker())
    monkeypatch.setattr(runner, "MonitorLoop", RateLimitedLoop)
    monkeypatch.setattr(runner, "_setup_logging", lambda path: logging.getLogger("test-rate-limit-runner"))
    monkeypatch.setattr(runner.signal, "signal", lambda signum, handler: None)
    monkeypatch.setattr(runner, "_maybe_run_study", lambda *args: None)
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
    stopped = {"value": False}

    def stop_after_cooldown():
        if sleeps:
            stopped["value"] = True
        return stopped["value"]

    result = runner.run_bot(config=BotConfig(), record_data=False, stop_flag=stop_after_cooldown)
    assert result.polls == 1
    assert sleeps == [300]
