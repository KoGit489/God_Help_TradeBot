"""Command-line entry point for the trading bot.

Provides the `god-help-tradebot` console command (after `pip install .`) and a
`python -m god_help_tradebot` fallback. Credentials come from environment
variables or a local `.env` file, so the same command works on a laptop and a
cloud server.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace

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
    parser.add_argument("--strategy", choices=("spy_0dte_options", "equity"), help="Override configured strategy mode")
    parser.add_argument("--config-file", default="bot_config.json", help="Path to bot_config.json overrides")
    parser.add_argument("--state-file", default="bot_state.json", help="Path to persisted exit plans")
    parser.add_argument("--options-state-file", default="spy_options_state.json", help="Path to persisted SPY option trade state")
    parser.add_argument("--options-performance-file", default="spy_options_performance.json", help="Path to persistent SPY budget and review history")
    parser.add_argument("--log-file", default="bot.log", help="Path to the rotating log file")
    parser.add_argument("--backtest", metavar="TIMELINE.json", help="Replay a JSON timeline instead of live trading")
    parser.add_argument("--backtest-cash", type=float, default=100_000, help="Starting cash for backtests")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load_bot_config(args.config_file)
    if args.strategy:
        config = replace(config, strategy_mode=args.strategy)
    if args.backtest:
        if config.strategy_mode != "equity":
            raise SystemExit("The current backtest engine is equity-only; pass --strategy equity. SPY options must be paper-run under supervision.")
        from .backtest import load_timeline, run_backtest

        result = run_backtest(
            load_timeline(args.backtest),
            config,
            starting_cash=args.backtest_cash,
            allow_shorts=config.allow_shorts,
        )
        print("=== BACKTEST RESULTS ===")
        print(f"trades: {result.total_trades}  wins: {result.wins}  losses: {result.losses}")
        print(f"win rate: {result.win_rate:.1%}")
        print(f"realized P&L: ${result.realized_pnl:,.2f}")
        for trade in result.trades:
            print(f"  {trade.symbol} {trade.side} x{trade.quantity}: {trade.entry_price} -> {trade.exit_price} = ${trade.pnl:,.2f}")
        return 0
    run_bot(
        config=config,
        poll_seconds=args.poll_seconds,
        max_polls=args.max_polls,
        allow_entries=not args.no_entries,
        use_events=args.events,
        min_confirmation_score=args.min_confirmation,
        state_file=args.state_file,
        options_state_file=args.options_state_file,
        options_performance_file=args.options_performance_file,
        log_file=args.log_file,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["main", "build_parser"]
