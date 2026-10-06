"""Run the bot end-to-end: connect, start the loop, and shut down cleanly.

`run_bot` wires the sandbox adapter to the monitoring loop with all research
layers on by default, persists exit plans to disk, and logs to a rotating file.
Ctrl+C stops the loop gracefully. Live mode remains locked off.
"""

from __future__ import annotations

import logging
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from .broker import WebullSandboxBroker
from .config import BotConfig
from .confirm import confirm_candidate
from .monitor import LoopEvent, LoopSummary, MonitorLoop
from .state import StateStore

LOG_FILE = Path("bot.log")


def _setup_logging(log_file: str | Path = LOG_FILE) -> logging.Logger:
    logger = logging.getLogger("god_help_tradebot")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logger.addHandler(console)
    return logger


def _log_events(logger: logging.Logger, summary: LoopSummary, seen: int) -> int:
    """Log new events since the last cycle; returns the new count."""
    for event in summary.events[seen:]:
        logger.info("[%s] %s: %s", event.kind, event.symbol, event.detail)
    return len(summary.events)


def run_bot(
    *,
    config: BotConfig | None = None,
    poll_seconds: float = 30.0,
    max_polls: int | None = None,
    allow_entries: bool = True,
    use_events: bool = False,
    min_confirmation_score: float = 0.5,
    state_file: str | Path = "bot_state.json",
    log_file: str | Path = LOG_FILE,
    stop_flag: Callable[[], bool] | None = None,
) -> LoopSummary:
    """Connect to the sandbox and run the monitoring loop until stopped.

    Exit plans persist to `state_file` so a restart resumes managing any open
    position. Errors inside a poll cycle are logged and the loop continues.
    """
    logger = _setup_logging(log_file)
    config = config or BotConfig()
    logger.info("bot starting: poll=%ss entries=%s", poll_seconds, allow_entries)

    broker = WebullSandboxBroker.from_env().connect()
    if not broker.sandbox_mode or broker.live_mode:
        raise RuntimeError("run_bot is sandbox-only; live mode is not enabled")

    loop = MonitorLoop(
        broker,
        config,
        confirmation=confirm_candidate,
        min_confirmation_score=min_confirmation_score,
        allow_entries=allow_entries,
        use_events=use_events,
        state_store=StateStore(state_file),
        poll_seconds=poll_seconds,
        max_polls=max_polls,
    )

    stop_requested = {"flag": False}

    def _handle_signal(signum, frame):  # noqa: ARG001
        stop_requested["flag"] = True
        logger.info("shutdown requested (signal %s)", signum)

    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            previous_handlers[sig] = signal.signal(sig, _handle_signal)
        except (ValueError, OSError):
            pass  # Not in the main thread (e.g. tests)

    summary = LoopSummary()
    seen_events = 0
    try:
        while max_polls is None or summary.polls < max_polls:
            if stop_requested["flag"] or (stop_flag and stop_flag()):
                logger.info("stopping after %s polls", summary.polls)
                break
            loop.run_once(summary)
            seen_events = _log_events(logger, summary, seen_events)
            if loop._consecutive_errors >= loop.max_consecutive_errors:
                logger.error("halting after %s consecutive errors", loop._consecutive_errors)
                break
            if max_polls is not None and summary.polls >= max_polls:
                break
            import time

            time.sleep(loop.error_backoff_seconds if loop._consecutive_errors else poll_seconds)
    finally:
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        logger.info("bot stopped: %s polls, %s entries, %s exits", summary.polls, len(summary.entries), len(summary.exits))

    return summary


__all__ = ["run_bot", "LOG_FILE"]
