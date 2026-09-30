from dataclasses import dataclass


@dataclass(frozen=True)
class BotConfig:
    """Explicit guardrails for a single planned trade."""

    max_symbol_price: float = 10.0
    risk_per_trade_usd: float = 25.0
    reward_multiple: float = 3.0
    max_position_value_usd: float = 500.0
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