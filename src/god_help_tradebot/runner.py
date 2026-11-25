"""Run the bot end-to-end: connect, start the loop, and shut down cleanly.

`run_bot` wires the sandbox adapter to the monitoring loop with all research
layers on by default, persists exit plans to disk, and logs to a rotating file.
Ctrl+C stops the loop gracefully. Live mode remains locked off.
"""

from __future__ import annotations

import logging
import signal
from decimal import Decimal
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Callable

from .broker import WebullSandboxBroker
from .config import BotConfig
from .confirm import confirm_candidate
from .monitor import LoopEvent, LoopSummary, MonitorLoop
from .recorder import SessionRecorder, list_recorded_days
from .state import StateStore, EquityPerformanceStore
from .study import load_study_state, run_study, save_study_state, write_config_file
from .schedule import get_trading_session

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


def _log_events(logger: logging.Logger, summary: LoopSummary, seen: int,
                review_loggers: dict[str, logging.Logger] | None = None) -> int:
    """Log new events since the last cycle; returns the new count."""
    for event in summary.events[seen:]:
        logger.info("[%s] %s: %s", event.kind, event.symbol, event.detail)
        if review_loggers:
            channel = "all" if event.kind in ("trade_completed", "trade_performance_review") else (
                "main" if event.kind.startswith("main_peak_") else
                "fallback" if event.kind.startswith("fallback_peak_") else None
            )
            if channel:
                review_loggers[channel].info("[%s] %s: %s", event.kind, event.symbol, event.detail)
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
    record_data: bool = True,
    data_dir: str | Path = "data",
    study_state_file: str | Path = "study_state.json",
    config_file: str | Path = "bot_config.json",
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
    if config.allow_shorts:
        broker.assert_margin_account()
    performance_store = None
    if config.trade_review_enabled and isinstance(broker, WebullSandboxBroker):
        performance_store = EquityPerformanceStore("equity_performance.json", broker._resolve_account_id())
        logger.info("[trade_review_ready] sandbox: completed=%s reviewed=%s target=4/5 paused=%s",
                len(performance_store.data["trades"]), performance_store.data["reviewed_count"], performance_store.data["paused"])
        broker.assert_no_remote_open_orders(allowed_client_order_ids=performance_store.recovery_ids)
    else:
        broker.assert_no_remote_open_orders()
    if config.daily_peak_mode and performance_store is None:
        raise RuntimeError("daily peak mode requires the persistent execution and review ledger")
    review_loggers = {}
    if config.daily_peak_mode:
        for channel, filename in (("all", "trade_accuracy.log"), ("main", "main_peak.log"), ("fallback", "fallback_peak.log")):
            review_logger = logging.getLogger(f"god_help_tradebot.review.{channel}")
            review_logger.setLevel(logging.INFO)
            review_logger.propagate = False
            for handler in list(review_logger.handlers):
                handler.close()
                review_logger.removeHandler(handler)
            handler = RotatingFileHandler(Path(log_file).parent / filename, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            review_logger.addHandler(handler)
            review_loggers[channel] = review_logger
        logger.warning("[daily_peak_mode] sandbox only; no fixed TP/SL; deadline 23:59 Eastern overrides losses; "
                       "unsupported sessions defer exits; exact peaks and fills are not guaranteed")

    loop = MonitorLoop(
        broker,
        config,
        confirmation=confirm_candidate,
        min_confirmation_score=min_confirmation_score,
        allow_entries=allow_entries,
        use_events=use_events,
        state_store=StateStore(state_file),
        performance_store=performance_store,
        recorder=SessionRecorder(data_dir) if record_data else None,
        poll_seconds=poll_seconds,
        max_polls=max_polls,
    )
    if config.daily_peak_mode:
        daily = performance_store.data["daily_reviews"]
        logger.info("[daily_reviews_ready] epoch=%s new_completed=%s main_graded=%s fallback_graded=%s reserve=$%s",
                    daily["started_at"], len(performance_store.data["trades"]) - daily["trade_offset"],
                    sum(result["strategy"] == "main" for result in daily["peak_results"].values()),
                    sum(result["strategy"] == "fallback" for result in daily["peak_results"].values()),
                    performance_store.data["profit_reserve_usd"])

    stop_requested = {"flag": False}
    research_worker = None
    execution_controller = None
    webull_history = None
    if config.research_enabled:
        from .research import ExecutionController, ResearchWorker
        if performance_store is None:
            raise ValueError("research promotion requires the account-bound performance ledger")
        root = Path(state_file).resolve().parent / "research"
        research_worker = ResearchWorker(root, Path(data_dir).resolve())
        execution_controller = ExecutionController(root)
        execution_controller.restore(loop)
        from .webull_history import WebullHistoryResearch
        if isinstance(broker, WebullSandboxBroker):
            webull_history = WebullHistoryResearch(root / "webull", broker.session.data_client.market_data.get_batch_history_bar,
                                                  broker.session.data_client)

    if config.account_price_priority:
        buying_power = loop._account_value()
        if buying_power is not None and Decimal(str(buying_power)).is_finite() and buying_power >= 0:
            logger.info("[account_buying_power] sandbox: startup buying power $%.2f", buying_power)
        else:
            logger.warning("[account_buying_power] sandbox: startup buying power unavailable; entry scans blocked until it recovers")

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
    halted_on_errors = False
    try:
        while max_polls is None or summary.polls < max_polls:
            if stop_requested["flag"] or (stop_flag and stop_flag()):
                logger.info("stopping after %s polls", summary.polls)
                break
            if execution_controller is not None:
                try:
                    execution_controller.tick(loop, summary)
                    research_worker.tick(loop._base_config)
                except Exception as error:
                    logger.warning("[research_wait] %s: research error; account execution continues", type(error).__name__)
            loop.run_once(summary)
            if webull_history is not None:
                try:
                    collection = webull_history.tick(loop)
                    if collection["status"] not in ("cooldown", "idle", "deferred_for_trading", "deferred_for_reconciliation"):
                        logger.info("[webull_history] %s", collection)
                except Exception as error:
                    logger.warning("[webull_history_wait] %s; account execution continues", type(error).__name__)
            seen_events = _log_events(logger, summary, seen_events, review_loggers)
            if performance_store is not None and max_polls is None and len(summary.events) > 1000:
                del summary.events[:-1000]
                seen_events = len(summary.events)
            if loop._consecutive_errors >= loop.max_consecutive_errors:
                logger.error("halting after %s consecutive errors", loop._consecutive_errors)
                halted_on_errors = True
                break
            if max_polls is not None and summary.polls >= max_polls:
                break
            _maybe_run_study(logger, config, data_dir, study_state_file, config_file)
            import time

            backoff = loop._consecutive_errors or getattr(loop, "_rate_limit_backoff_active", False)
            interval = min(poll_seconds, 5.0) if config.daily_peak_mode and (loop.exit_plans or loop._pending_limit) else poll_seconds
            time.sleep(loop.error_backoff_seconds if backoff else interval)
    finally:
        if research_worker is not None:
            research_worker.close()
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass
        logger.info("bot stopped: %s polls, %s entries, %s exits", summary.polls, len(summary.entries), len(summary.exits))

    if halted_on_errors:
        raise RuntimeError(
            f"bot halted after {loop._consecutive_errors} consecutive poll errors"
        )

    return summary


__all__ = ["run_bot", "LOG_FILE"]


def _maybe_run_study(
    logger: logging.Logger,
    config: BotConfig,
    data_dir: str | Path,
    study_state_file: str | Path,
    config_file: str | Path,
) -> None:
    """After the close on every Nth recorded day, grade the window and self-improve."""
    if config.trade_review_enabled:
        return
    from datetime import datetime

    from .schedule import EASTERN

    now = datetime.now(EASTERN)
    session = get_trading_session(now.date())
    if session is None or now < session.market_close:
        return

    state = load_study_state(study_state_file)
    recorded_days = len(list_recorded_days(data_dir))
    studied_through = int(state.get("studied_through", 0))
    if recorded_days - studied_through < config.study_interval_days:
        return

    logger.info("study starting: %s recorded days", recorded_days)
    report = run_study(
        config,
        data_dir=data_dir,
        interval_days=config.study_interval_days,
        target_winning_days=config.study_target_winning_days,
        auto_improve=config.study_auto_improve and not config.trade_review_enabled,
        config_writer=lambda improved: write_config_file(improved, config_file),
    )
    logger.info(
        "study done: %s/%s winning days in window (target %s)",
        report.window_winning_days,
        report.window_total_days,
        config.study_target_winning_days,
    )
    if report.change_applied:
        logger.info("study improvement applied: %s", report.change_applied)
    save_study_state({"studied_through": recorded_days}, study_state_file)
