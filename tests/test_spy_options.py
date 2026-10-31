from datetime import datetime
from decimal import Decimal

from god_help_tradebot import BotConfig
from god_help_tradebot.botconfig import load_bot_config
from god_help_tradebot.confirm import ConfirmationReport
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.spy_options import (
    OptionContract,
    decide_option_direction,
    parse_contract,
    parse_option_quote,
    select_option_contract,
    select_weak_fallback_contract,
)


def _report(score: float, signals: int = 4) -> ConfirmationReport:
    return ConfirmationReport("SPY", score, 0.0, 0.7, 0.6, signals_present=signals, passed=True)


def _contract(symbol: str, option_type: str, strike: str = "680") -> OptionContract:
    return OptionContract(symbol, "2026-10-28", Decimal(strike), option_type)


def _snapshot(symbol: str, ask: str, *, bid: str = "0.80", delta: str = "0.30", delay: str = "0") -> dict:
    return {
        "symbol": symbol,
        "bid": bid,
        "ask": ask,
        "bid_size": "100",
        "ask_size": "120",
        "delta": delta,
        "gamma": "0.05",
        "theta": "-0.8",
        "vega": "0.03",
        "rho": "0.001",
        "imp_vol": "0.25",
        "quote_time": "2026-10-28T15:00:00Z",
        "delay_minutes": delay,
        "volume": "500",
    }


def test_signal_maps_strong_and_weak_consensus_as_requested() -> None:
    config = BotConfig()
    assert decide_option_direction(_report(.82), _report(.45), config, trend="up").option_type == "CALL"
    assert decide_option_direction(_report(.62), _report(.50), config, trend=None).option_type == "PUT"
    assert decide_option_direction(_report(.50), _report(.62), config, trend=None).option_type == "CALL"
    assert decide_option_direction(_report(.43), _report(.81), config, trend="down").option_type == "PUT"


def test_signal_waits_on_neutral_or_insufficient_data() -> None:
    config = BotConfig()
    assert decide_option_direction(_report(.55), _report(.53), config, trend=None).option_type is None
    assert decide_option_direction(_report(.85, 1), _report(.20, 1), config, trend="up").option_type is None


def test_selects_highest_ask_under_one_dollar_with_liquidity_and_greeks() -> None:
    contracts = [
        _contract("SPY261028C00680000", "CALL"),
        _contract("SPY261028C00681000", "CALL"),
        _contract("SPY261028C00682000", "CALL"),
    ]
    snapshots = {
        contracts[0].option_symbol: _snapshot(contracts[0].option_symbol, "0.70"),
        contracts[1].option_symbol: _snapshot(contracts[1].option_symbol, "1.01"),
        contracts[2].option_symbol: _snapshot(contracts[2].option_symbol, "0.95", bid="0.90"),
    }

    result = select_option_contract(contracts, snapshots, "CALL", BotConfig(), datetime(2026, 10, 28, 11, 0, tzinfo=EASTERN))

    assert result.contract == contracts[2]
    assert result.quote.ask == Decimal("0.95")


def test_dynamic_budget_selects_most_expensive_affordable_contract() -> None:
    contracts = [
        _contract("SPY261028C00680000", "CALL"),
        _contract("SPY261028C00681000", "CALL"),
        _contract("SPY261028C00682000", "CALL"),
    ]
    snapshots = {
        contracts[0].option_symbol: _snapshot(contracts[0].option_symbol, "0.75", bid="0.70"),
        contracts[1].option_symbol: _snapshot(contracts[1].option_symbol, "1.25", bid="1.10"),
        contracts[2].option_symbol: _snapshot(contracts[2].option_symbol, "1.31", bid="1.20"),
    }
    now = datetime(2026, 10, 28, 11, 0, tzinfo=EASTERN)

    selection_130 = select_option_contract(contracts, snapshots, "CALL", BotConfig(), now, budget_usd=Decimal("130"))
    selection_80 = select_option_contract(contracts, snapshots, "CALL", BotConfig(), now, budget_usd=Decimal("80"))

    assert selection_130.contract == contracts[1]
    assert selection_80.contract == contracts[0]


def test_weak_fallback_relaxes_greeks_and_spread_but_keeps_hard_quote_gates() -> None:
    contract = _contract("SPY261028P00680000", "PUT")
    quote = _snapshot(contract.option_symbol, "0.90", bid="0.60", delta="-0.30") | {
        "gamma": None,
        "theta": None,
        "vega": None,
        "rho": None,
        "imp_vol": None,
        "volume": "0",
    }
    now = datetime(2026, 10, 28, 11, 0, tzinfo=EASTERN)
    strict = select_option_contract(
        [contract], {contract.option_symbol: quote}, "PUT", BotConfig(), now,
        budget_usd=Decimal("100"),
    )
    fallback = select_weak_fallback_contract(
        [contract], {contract.option_symbol: quote}, "PUT", BotConfig(), now,
        budget_usd=Decimal("100"),
    )

    assert strict.contract is None
    assert fallback.contract == contract
    assert "weak-signal fallback" in fallback.reason

    stale = quote | {"delay_minutes": "3"}
    rejected = select_weak_fallback_contract(
        [contract], {contract.option_symbol: stale}, "PUT", BotConfig(), now,
        budget_usd=Decimal("100"),
    )
    assert rejected.contract is None
    assert "stale_quote=1" in rejected.reason


def test_legacy_budget_setting_seeds_new_strategy_budget(tmp_path) -> None:
    config_file = tmp_path / "bot_config.json"
    config_file.write_text('{"spy_option_contract_budget_usd": 130}', encoding="utf-8")

    assert load_bot_config(config_file).spy_option_initial_budget_usd == 130


def test_contract_selector_rejects_missing_greeks_depth_and_stale_quotes() -> None:
    contract = _contract("SPY261028C00680000", "CALL")
    no_greeks = {"symbol": contract.option_symbol, "bid": "0.8", "ask": "0.9", "bid_size": "2", "ask_size": "3"}
    zero_depth = _snapshot(contract.option_symbol, "0.9") | {"ask_size": "0"}
    zero_volume = _snapshot(contract.option_symbol, "0.9") | {"volume": "0"}
    stale = _snapshot(contract.option_symbol, "0.9", delay="3")
    selection_time = datetime(2026, 10, 28, 11, 0, tzinfo=EASTERN)

    for raw in (no_greeks, zero_depth, zero_volume, stale):
        result = select_option_contract([contract], {contract.option_symbol: raw}, "CALL", BotConfig(), selection_time)
        assert result.contract is None


def test_option_quote_parser_preserves_greeks_and_sizes() -> None:
    contract = _contract("SPY261028P00680000", "PUT")
    raw = _snapshot(contract.option_symbol, "0.8", delta="-0.3")
    quote = parse_option_quote(raw)
    assert quote is not None
    assert quote.delta == -0.3
    assert quote.gamma == 0.05
    assert quote.theta == -0.8
    assert quote.vega == 0.03
    assert quote.bid_size == Decimal("100")


def test_contract_parser_accepts_only_standard_100_multiplier() -> None:
    standard = {
        "symbol": "SPY261028C00680000", "expiration_date": "2026-10-28",
        "strike_price": "680", "option_type": "CALL", "multiplier": "100",
    }
    assert parse_contract(standard) == _contract("SPY261028C00680000", "CALL")
    assert parse_contract(standard | {"multiplier": "10"}) is None
