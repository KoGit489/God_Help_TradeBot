"""Command-line entry point for the trading bot.

Provides the `god-help-tradebot` console command (after `pip install .`) and a
`python -m god_help_tradebot` fallback. Credentials come from environment
variables or a local `.env` file, so the same command works on a laptop and a
cloud server.
"""

from __future__ import annotations

import argparse
import sys

from .botconfig import load_bot_config
from .runner import run_bot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="god-help-tradebot",
        description="Run the God Help TradeBot monitoring loop (sandbox-only).",
    )
    parser.add_argument("--poll-seconds", type=float, default=30.0, help="Seconds between poll cycles")
    parser.add_argument("--max-polls", type=int, default=None, help="Stop after N polls (default: run until stopped)")
    parser.add_argument("--no-entries", action="store_true", help="Observe only; never enter positions")
    parser.add_argument("--events", action="store_true", help="Also subscribe to gRPC order events (polling still reconciles)")
    parser.add_argument("--min-confirmation", type=float, default=0.5, help="Minimum confirmation score to enter")
    parser.add_argument("--config-file", default="bot_config.json", help="Path to bot_config.json overrides")
    parser.add_argument("--state-file", default="bot_state.json", help="Path to persisted exit plans")
    parser.add_argument("--log-file", default="bot.log", help="Path to the rotating log file")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_bot_config(args.config_file)
    run_bot(
        config=config,
        poll_seconds=args.poll_seconds,
        max_polls=args.max_polls,
        allow_entries=not args.no_entries,
        use_events=args.events,
        min_confirmation_score=args.min_confirmation,
        state_file=args.state_file,
        log_file=args.log_file,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "build_parser"]
