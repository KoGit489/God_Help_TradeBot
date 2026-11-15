from decimal import Decimal

import pytest

from god_help_tradebot import BotConfig, build_trade_plan
from god_help_tradebot.risk import build_short_plan


@pytest.mark.parametrize("build,stop", [(build_trade_plan, 270.0), (build_short_plan, 330.0)])
def test_account_price_priority_uses_buying_power_above_legacy_price_cap(build, stop):
    config = BotConfig(account_price_priority=True)
    plan = build(300.0, stop, config, account_value=100_000)
    assert plan.quantity == 326
    assert plan.entry_price * plan.quantity <= Decimal("98000")
    assert build(300.0, stop, config, account_value=1_000).quantity == 3


@pytest.mark.parametrize("buying_power", [None, 0, -1, float("nan"), float("inf")])
def test_account_price_priority_never_falls_back_to_fixed_caps(buying_power):
    for build, stop in ((build_trade_plan, 270.0), (build_short_plan, 330.0)):
        with pytest.raises(ValueError, match="buying power"):
            build(300.0, stop, BotConfig(account_price_priority=True), account_value=buying_power)


def test_account_price_priority_preserves_share_cap_and_allows_sub_dollar_prices():
    plan = build_trade_plan(0.50, 0.45, BotConfig(account_price_priority=True), account_value=1_000_000)
    assert plan.quantity == 199_999


@pytest.mark.parametrize("buying_power", [None, Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_entry_scan_is_blocked_without_spendable_buying_power(monkeypatch, buying_power):
    from god_help_tradebot import MonitorLoop, PaperBroker
    from god_help_tradebot.monitor import LoopSummary

    broker = PaperBroker(1_000)
    broker.get_buying_power = lambda: buying_power
    loop = MonitorLoop(broker, BotConfig(account_price_priority=True))
    monkeypatch.setattr(loop, "_market_open", lambda: True)
    monkeypatch.setattr(loop, "_in_entry_cooldown", lambda: False)
    monkeypatch.setattr(loop, "_gather_snapshots", lambda: pytest.fail("must not scan without buying power"))
    summary = LoopSummary()
    loop._maybe_enter(summary)
    assert summary.events[-1].kind == "sizing_blocked"


def test_buying_power_is_refreshed_before_each_candidate_scan(monkeypatch):
    from god_help_tradebot import MonitorLoop, PaperBroker
    from god_help_tradebot.monitor import LoopSummary

    broker = PaperBroker(1_000)
    funds = iter([Decimal("1000000"), Decimal("500")])
    broker.get_buying_power = lambda: next(funds)
    loop = MonitorLoop(broker, BotConfig(account_price_priority=True))
    monkeypatch.setattr(loop, "_market_open", lambda: True)
    monkeypatch.setattr(loop, "_in_entry_cooldown", lambda: False)
    monkeypatch.setattr(loop, "_gather_snapshots", lambda: [])
    summary = LoopSummary()
    loop._maybe_enter(summary)
    loop._maybe_enter(summary)
    assert "$1,000,000.00" in summary.events[0].detail
    assert "$500.00" in summary.events[1].detail


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


def test_long_and_short_trade_plans_enforce_minimum_symbol_price() -> None:
    from god_help_tradebot.risk import build_short_plan

    config = BotConfig(allow_shorts=True)
    for build, entry, stop in ((build_trade_plan, 0.99, 0.90), (build_short_plan, 0.99, 1.10)):
        try:
            build(entry, stop, config)
            raise AssertionError("should enforce minimum symbol price")
        except ValueError as exc:
            assert "minimum" in str(exc)
