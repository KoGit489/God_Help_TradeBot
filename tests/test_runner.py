import logging
from types import SimpleNamespace

import pytest

from god_help_tradebot import BotConfig
from god_help_tradebot import runner
from god_help_tradebot.broker import WebullSandboxBroker
import god_help_tradebot.spy_options as spy_options


class _Broker:
    sandbox_mode = True
    live_mode = False

    def connect(self):
        return self

    def assert_no_remote_open_orders(self, **kwargs):
        pass


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
            config=BotConfig(strategy_mode="equity"),
            state_file=tmp_path / "state.json",
            log_file=tmp_path / "bot.log",
        )


def test_default_runner_dispatches_to_spy_options_loop(monkeypatch, tmp_path) -> None:
    calls = []

    class _OptionsLoop:
        max_consecutive_errors = 5
        error_backoff_seconds = 0

        def __init__(self, broker, config, **kwargs):
            calls.append((config.strategy_mode, kwargs.get("state_file"), kwargs.get("performance_file")))
            self.recovery_order_ids = frozenset()
            self._consecutive_errors = 0

        def run_once(self, summary):
            summary.polls += 1
            return summary

    monkeypatch.setattr(runner.WebullSandboxBroker, "from_env", lambda: _Broker())
    monkeypatch.setattr(spy_options, "SPY0DTEOptionsLoop", _OptionsLoop)
    monkeypatch.setattr(runner, "_setup_logging", lambda path: logging.getLogger("test-options-runner"))
    monkeypatch.setattr(runner.signal, "signal", lambda signum, handler: None)

    runner.run_bot(
        config=BotConfig(), max_polls=1, options_state_file=tmp_path / "spy_options.json",
        options_performance_file=tmp_path / "spy_options_performance.json",
    )

    assert calls == [(
        "spy_0dte_options", tmp_path / "spy_options.json", tmp_path / "spy_options_performance.json",
    )]


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
