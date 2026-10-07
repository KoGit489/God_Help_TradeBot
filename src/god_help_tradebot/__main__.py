"""Allow `python -m god_help_tradebot` to launch the bot."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
