import logging

import pytest

from god_help_tradebot import BotConfig
from god_help_tradebot import runner


class _Broker:
    sandbox_mode = True
    live_mode = False

    def connect(self):
        return self


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
