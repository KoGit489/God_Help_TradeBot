from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

from .config import BotConfig


@dataclass(frozen=True)
class TradePlan:
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    quantity: int
    risk_usd: Decimal


def build_trade_plan(
    entry_price: float,
    stop_price: float,
    config: BotConfig,
    account_value: float | None = None,
) -> TradePlan:
    entry = Decimal(str(entry_price))
    stop = Decimal(str(stop_price))
    if entry <= 0 or stop <= 0:
        raise ValueError("prices must be positive")
    if stop >= entry:
        raise ValueError("stop_price must be below entry_price for a long trade")
    if entry > Decimal(str(config.max_symbol_price)):
        raise ValueError("entry price exceeds the configured symbol price limit")

    if config.position_pct_of_account > 0 and account_value is not None:
        # Account-percentage mode: deploy that fraction of the account; the stop
        # distance defines the dollar risk, and the bracket caps the downside.
        max_value = Decimal(str(account_value)) * Decimal(str(config.position_pct_of_account))
        quantity = int((max_value / entry).to_integral_value(rounding=ROUND_DOWN))
    else:
        risk_per_share = entry - stop
        configured_risk = Decimal(str(config.risk_per_trade_usd))
        quantity_by_risk = int((configured_risk / risk_per_share).to_integral_value(rounding=ROUND_DOWN))
        max_value = Decimal(str(config.max_position_value_usd))
        quantity_by_value = int((max_value / entry).to_integral_value(rounding=ROUND_DOWN))
        quantity = min(quantity_by_risk, quantity_by_value)
    if quantity < 1:
        raise ValueError("configured limits do not allow one share at this stop distance")

    risk_per_share = entry - stop
    target = entry + (risk_per_share * Decimal(str(config.reward_multiple)))
    actual_risk = (risk_per_share * quantity).quantize(Decimal("0.01"))
    return TradePlan(entry, stop, target, quantity, actual_risk)