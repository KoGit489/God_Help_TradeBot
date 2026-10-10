from decimal import Decimal

from god_help_tradebot import BotConfig, build_trade_plan


def test_pct_mode_deploys_fraction_of_account() -> None:
    config = BotConfig(position_pct_of_account=1.0)
    plan = build_trade_plan(2.0, 1.8, config, account_value=100_000)

    # Full account at $2.00 with the 2% market-order headroom = 49,000 shares.
    assert plan.quantity == 49_000
    assert plan.risk_usd == Decimal("9800.00")


def test_pct_mode_half_account() -> None:
    config = BotConfig(position_pct_of_account=0.5)
    plan = build_trade_plan(2.0, 1.8, config, account_value=100_000)

    assert plan.quantity == 24_500


def test_pct_mode_caps_order_quantity_below_platform_limit() -> None:
    config = BotConfig(position_pct_of_account=1.0)
    plan = build_trade_plan(1.0, 0.9, config, account_value=1_000_000)

    assert plan.quantity == 199_999


def test_fixed_mode_ignores_account_value() -> None:
    config = BotConfig()  # position_pct_of_account defaults to 0
    plan = build_trade_plan(2.0, 1.8, config, account_value=1_000_000)

    # Fixed caps rule: risk $25 / $0.20 = 125 shares, value cap 500/2 = 250 -> 125.
    assert plan.quantity == 125


def test_pct_mode_without_account_value_falls_back_to_fixed() -> None:
    config = BotConfig(position_pct_of_account=1.0)
    plan = build_trade_plan(2.0, 1.8, config, account_value=None)

    assert plan.quantity == 125


def test_pct_mode_rejects_out_of_range() -> None:
    try:
        BotConfig(position_pct_of_account=1.5)
        raise AssertionError("should reject pct > 1")
    except ValueError:
        pass


def test_pct_mode_still_enforces_symbol_price_limit() -> None:
    config = BotConfig(position_pct_of_account=1.0, max_symbol_price=5.0)
    try:
        build_trade_plan(20.0, 18.0, config, account_value=1_000_000)
        raise AssertionError("should enforce max_symbol_price")
    except ValueError:
        pass
