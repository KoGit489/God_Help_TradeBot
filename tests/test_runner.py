import logging
from types import SimpleNamespace

import pytest

from god_help_tradebot import BotConfig
from god_help_tradebot import runner
from god_help_tradebot.broker import WebullSandboxBroker


class _Broker:
    sandbox_mode = True
    live_mode = False

    def connect(self):
        return self

    def assert_no_remote_open_orders(self):
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
