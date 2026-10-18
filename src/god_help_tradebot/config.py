from dataclasses import dataclass


@dataclass(frozen=True)
class BotConfig:
    """Explicit guardrails for a single planned trade."""

    max_symbol_price: float = 10.0
    risk_per_trade_usd: float = 25.0
    reward_multiple: float = 3.0
    max_position_value_usd: float = 500.0
    min_change_percent: float = 2.0
    max_change_percent: float = 40.0
    min_relative_volume: float = 2.0
    min_average_volume: int = 100_000
    max_spread_percent: float = 1.0
    paper_mode: bool = True
    # When > 0, deploy this fraction of the account's value per trade instead of
    # the fixed dollar caps. The stop distance then defines the dollar risk.
    position_pct_of_account: float = 0.0
    # Allow short entries on fade setups (requires a margin account).
    allow_shorts: bool = False
    # Bracket distances from the entry price (fixed percentages).
    stop_pct: float = 0.10
    target_pct: float = 0.30
    # Accuracy gates: require real signal data and tape-aligned entries.
    min_signals_present: int = 2
    require_trend_alignment: bool = True
    # Forced best-odds entry when nothing passes (keeps trading every session).
    allow_fallback: bool = True
    # No entries for this many minutes after the 9:30 open (widest spreads,
    # fakest moves live there).
    entry_cooldown_minutes: int = 15
    # Candidates must pass confirmation on two consecutive polls before entry.
    require_double_confirmation: bool = True
    # Coil-not-spike: momentum entries already up more than this are too late.
    momentum_max_change_percent: float = 20.0
    # Patient entries: rest a limit this fraction inside the spread instead of
    # paying the full ask; chase at market after this many polls if unfilled.
    entry_limit_spread_fraction: float = 0.25
    entry_chase_polls: int = 2
    # Pre-pop candidates are early by definition: one confirmation pass suffices.
    prepop_fast_trigger: bool = True
    # Paradox fallback: low-conviction fallback reads get traded the OPPOSITE
    # way (bullish survivor -> short it, bearish survivor -> buy it). Genuine
    # gate-passers always go with the grain.
    paradox_fallback: bool = True
    # Fallback entries wait for the tape to develop: none in the first hour
    # after the open (the morning chaos is where weak reads lie most).
    fallback_after_minutes: int = 60
    # Paradox tape guard: never fade a read the tape is actively confirming —
    # paradox shorts stand down on an up tape, paradox longs on a down tape.
    paradox_trend_guard: bool = True
    # Wide-net mode: rows pulled per screener list, and the max deep
    # confirmations per poll cycle (protects the news quota on wide nets).
    screener_list_limit: int = 100
    max_confirmations_per_cycle: int = 5
    # Self-study cadence: every N recorded trading days, grade the strategy and
    # target at least this many winning days in the window.
    study_interval_days: int = 5
    study_target_winning_days: int = 4
    study_auto_improve: bool = True

    def __post_init__(self) -> None:
        if self.max_symbol_price <= 0:
            raise ValueError("max_symbol_price must be positive")
        if self.risk_per_trade_usd <= 0:
            raise ValueError("risk_per_trade_usd must be positive")
        if self.reward_multiple < 1:
            raise ValueError("reward_multiple must be at least 1")
        if self.max_position_value_usd <= 0:
            raise ValueError("max_position_value_usd must be positive")
        if self.max_change_percent < self.min_change_percent:
            raise ValueError("change percent bounds are invalid")
        if self.min_relative_volume <= 0:
            raise ValueError("min_relative_volume must be positive")
        if self.min_average_volume <= 0:
            raise ValueError("min_average_volume must be positive")
        if self.max_spread_percent <= 0:
            raise ValueError("max_spread_percent must be positive")
        if not 0.0 <= self.position_pct_of_account <= 1.0:
            raise ValueError("position_pct_of_account must be between 0 and 1")
        if self.momentum_max_change_percent < self.min_change_percent:
            raise ValueError("momentum_max_change_percent must be >= min_change_percent")
        if not 0.0 <= self.entry_limit_spread_fraction <= 1.0:
            raise ValueError("entry_limit_spread_fraction must be between 0 and 1")
        if self.entry_chase_polls < 0:
            raise ValueError("entry_chase_polls cannot be negative")
        if self.fallback_after_minutes < 0:
            raise ValueError("fallback_after_minutes cannot be negative")
        if self.screener_list_limit < 1:
            raise ValueError("screener_list_limit must be at least 1")
        if self.max_confirmations_per_cycle < 1:
            raise ValueError("max_confirmations_per_cycle must be at least 1")
        if not 0.0 < self.stop_pct < 1.0:
            raise ValueError("stop_pct must be between 0 and 1")
        if not 0.0 < self.target_pct < 1.0:
            raise ValueError("target_pct must be between 0 and 1")
        if self.min_signals_present < 0 or self.min_signals_present > 4:
            raise ValueError("min_signals_present must be between 0 and 4")
        if self.entry_cooldown_minutes < 0:
            raise ValueError("entry_cooldown_minutes cannot be negative")
        if self.study_interval_days < 1:
            raise ValueError("study_interval_days must be at least 1")
        if self.study_target_winning_days < 1:
            raise ValueError("study_target_winning_days must be at least 1")