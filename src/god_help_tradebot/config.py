from dataclasses import dataclass


@dataclass(frozen=True)
class BotConfig:
    """Explicit guardrails for a single planned trade."""

    strategy_mode: str = "spy_0dte_options"

    min_symbol_price: float = 1.0
    max_symbol_price: float = 20.0
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
    # Legacy entry settings retained for config compatibility; market-only
    # execution no longer uses a limit price or entry chase delay.
    entry_limit_spread_fraction: float = 0.25
    entry_chase_polls: int = 2
    # Exit watchdog: an unfilled exit order is chased aggressively after this
    # many polls — a submitted exit is a promise, not a hope.
    exit_chase_polls: int = 2
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
    # Rows used from each non-paginated ranking (API maximum 200), and
    # maximum deep confirmations per poll cycle.
    screener_list_limit: int = 200
    max_confirmations_per_cycle: int = 5
    # Self-study cadence: every N recorded trading days, grade the strategy and
    # target at least this many winning days in the window.
    study_interval_days: int = 5
    study_target_winning_days: int = 4
    study_auto_improve: bool = True
    # SPY 0DTE long-option paper strategy. Premium cap is per standard
    # 100-share contract; paper sizing uses available buying power.
    spy_option_contract_budget_usd: float = 100.0
    spy_option_buying_power_fraction: float = 0.98
    spy_option_max_spread_percent: float = 20.0
    spy_option_min_abs_delta: float = 0.05
    spy_option_max_abs_delta: float = 0.65
    spy_option_strong_score: float = 0.70
    spy_option_weak_floor: float = 0.55
    spy_option_min_direction_gap: float = 0.05
    spy_option_strong_direction_gap: float = 0.15
    spy_option_max_strike_distance: float = 25.0
    spy_option_min_signal_count: int = 2
    spy_option_max_quote_delay_minutes: int = 1
    spy_option_max_contracts_per_order: int = 50_000
    spy_option_scan_minutes_before_open: int = 5

    def __post_init__(self) -> None:
        if self.strategy_mode not in ("spy_0dte_options", "equity"):
            raise ValueError("strategy_mode must be spy_0dte_options or equity")
        if self.min_symbol_price <= 0:
            raise ValueError("min_symbol_price must be positive")
        if self.max_symbol_price < self.min_symbol_price:
            raise ValueError("max_symbol_price must be >= min_symbol_price")
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
        if self.exit_chase_polls < 1:
            raise ValueError("exit_chase_polls must be at least 1")
        if self.fallback_after_minutes < 0:
            raise ValueError("fallback_after_minutes cannot be negative")
        if not 1 <= self.screener_list_limit <= 200:
            raise ValueError("screener_list_limit must be between 1 and the API maximum of 200")
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
        if self.spy_option_contract_budget_usd <= 0:
            raise ValueError("spy_option_contract_budget_usd must be positive")
        if not 0 < self.spy_option_buying_power_fraction <= 1:
            raise ValueError("spy_option_buying_power_fraction must be in (0, 1]")
        if self.spy_option_max_spread_percent <= 0:
            raise ValueError("spy_option_max_spread_percent must be positive")
        if not 0 <= self.spy_option_min_abs_delta <= self.spy_option_max_abs_delta <= 1:
            raise ValueError("SPY option delta bounds must be ordered within [0, 1]")
        if not 0.5 <= self.spy_option_weak_floor < self.spy_option_strong_score <= 1:
            raise ValueError("SPY option signal thresholds must satisfy 0.5 <= weak < strong <= 1")
        if not 0 <= self.spy_option_min_direction_gap <= self.spy_option_strong_direction_gap <= 1:
            raise ValueError("SPY option direction gaps must be ordered within [0, 1]")
        if self.spy_option_max_strike_distance <= 0:
            raise ValueError("spy_option_max_strike_distance must be positive")
        if self.spy_option_min_signal_count < 1:
            raise ValueError("spy_option_min_signal_count must be positive")
        if self.spy_option_max_quote_delay_minutes < 0:
            raise ValueError("spy_option_max_quote_delay_minutes cannot be negative")
        if self.spy_option_max_contracts_per_order < 1:
            raise ValueError("spy_option_max_contracts_per_order must be positive")
        if self.spy_option_scan_minutes_before_open < 0:
            raise ValueError("spy_option_scan_minutes_before_open cannot be negative")