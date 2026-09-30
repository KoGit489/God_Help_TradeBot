from decimal import Decimal

import pytest

from god_help_tradebot import BotConfig, build_trade_plan


def test_trade_plan_respects_risk_and_position_caps() -> None:
    plan = build_trade_plan(2.00, 1.90, BotConfig(risk_per_trade_usd=25, max_position_value_usd=100))

    assert plan.quantity == 50
    assert plan.target_price == Decimal("2.30")
    assert plan.risk_usd == Decimal("5.00")


def test_trade_plan_rejects_invalid_stop() -> None:
    with pytest.raises(ValueError, match="stop_price"):
        build_trade_plan(2.00, 2.10, BotConfig())


def test_trade_plan_rejects_price_above_limit() -> None:
    with pytest.raises(ValueError, match="price limit"):
        build_trade_plan(10.01, 9.90, BotConfig())