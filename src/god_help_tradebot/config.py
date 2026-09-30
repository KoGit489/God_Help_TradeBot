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

    def __post_init__(self) -> None:
        if self.max_symbol_price <= 0:
            raise ValueError("max_symbol_price must be positive")
        if self.risk_per_trade_usd <= 0:
            raise ValueError("risk_per_trade_usd must be positive")
        if self.reward_multiple < 1:
            raise ValueError("reward_multiple must be at least 1")
        if self.max_position_value_usd <= 0:
            raise ValueError("max_position_value_usd must be positive")
        if self.min_change_percent < 0 or self.max_change_percent < self.min_change_percent:
            raise ValueError("change percent bounds are invalid")
        if self.min_relative_volume <= 0:
            raise ValueError("min_relative_volume must be positive")
        if self.min_average_volume <= 0:
            raise ValueError("min_average_volume must be positive")
        if self.max_spread_percent <= 0:
            raise ValueError("max_spread_percent must be positive")