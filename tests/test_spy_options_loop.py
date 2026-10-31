from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from god_help_tradebot import BotConfig, Quote
from god_help_tradebot.monitor import LoopSummary
from god_help_tradebot.paper import OrderSide, OrderType, PaperOrder
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.spy_options import OptionContract, OptionSignal, SPY0DTEOptionsLoop


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Broker:
    sandbox_mode = True
    live_mode = False

    def __init__(self, option_quote):
        self.option_quote = option_quote
        self.orders = []

    def get_positions(self):
        return {}

    def get_latest_quote(self, symbol):
        return Quote("SPY", Decimal("600.00"), Decimal("600.01"), Decimal("600.00"))

    def get_option_contracts(self, underlying, expiry, *, strike_min, strike_max, pagination_key=None):
        return _Response({"data": [{
            "symbol": "SPY261028C00600000", "expiration_date": expiry,
            "strike_price": "600", "option_type": "CALL", "multiplier": "100",
        }]})

    def get_option_snapshots(self, symbols):
        return {self.option_quote["symbol"]: self.option_quote}

    def get_buying_power(self):
        return Decimal("10000")

    def submit_option_market_order(self, underlying, option_symbol, strike, expiry, option_type,
                                   quantity, *, position_intent, client_order_id=None):
        self.orders.append((option_symbol, quantity, position_intent, client_order_id))
        return PaperOrder(1, option_symbol, OrderSide.BUY, quantity, OrderType.MARKET,
                          client_order_id=client_order_id)


def _option_quote(**overrides):
    value = {
        "symbol": "SPY261028C00600000", "bid": "0.85", "ask": "0.90",
        "bid_size": "1000", "ask_size": "1000", "delta": "0.30",
        "gamma": "0.05", "theta": "-0.8", "vega": "0.03", "rho": "0.001",
        "imp_vol": "0.25", "quote_time": "2026-10-28T14:00:00Z",
        "delay_minutes": "0", "volume": "500",
    }
    value.update(overrides)
    return value


def test_spy_options_loop_submits_max_affordable_market_contracts(tmp_path, monkeypatch):
    import god_help_tradebot.spy_options as strategy

    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    broker = _Broker(_option_quote())
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options", spy_option_initial_budget_usd=10000),
        state_file=tmp_path / "spy_options_state.json", now_provider=lambda: now,
    )
    monkeypatch.setattr(strategy, "fetch_market_news", lambda: None)
    monkeypatch.setattr(strategy, "confirm_candidate", lambda *a, **k: SimpleNamespace(score=.82, signals_present=4))
    monkeypatch.setattr(strategy, "confirm_candidate_bearish", lambda *a, **k: SimpleNamespace(score=.30, signals_present=4))
    monkeypatch.setattr(strategy, "trend_direction", lambda *a, **k: "up")
    loop._market_news = None
    loop._evaluate_signal = lambda: OptionSignal("CALL", "strong_bullish", "test", .82, .30, 4, "up")

    summary = loop.run_once()

    assert len(broker.orders) == 1
    assert broker.orders[0][1] == 111  # maximum affordable at a $0.90 ask
    assert broker.orders[0][2] == "BUY_TO_OPEN"
    assert any(event.kind == "spy_option_entry" for event in summary.events)
    assert loop._state["status"] == "ENTRY_PENDING"


def test_default_strategy_budget_is_independent_of_larger_account_balance(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    broker = _Broker(_option_quote())
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json", now_provider=lambda: now,
    )
    loop._evaluate_signal = lambda: OptionSignal("CALL", "strong_bullish", "test", .82, .30, 4, "up")

    loop.run_once()

    assert broker.orders[0][1] == 1
    assert loop._performance["strategy_equity_usd"] == "100.00"


def test_spy_options_loop_retries_quantity_rejection_at_half_size(tmp_path, monkeypatch):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    broker = _Broker(_option_quote())
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options", spy_option_initial_budget_usd=10000),
        state_file=tmp_path / "spy_options_state.json", now_provider=lambda: now,
    )
    loop._evaluate_signal = lambda: OptionSignal("CALL", "strong_bullish", "test", .82, .30, 4, "up")
    submit = broker.submit_option_market_order
    requested_quantities = []

    def reject_once(*args, **kwargs):
        requested_quantities.append(args[5])
        if len(requested_quantities) == 1:
            raise RuntimeError("ORDER_RISK_RULE_OPTIONS: maximum quantity exceeded")
        return submit(*args, **kwargs)

    monkeypatch.setattr(broker, "submit_option_market_order", reject_once)

    summary = loop.run_once()

    assert requested_quantities == [111, 55]
    assert broker.orders[0][1] == 55
    assert loop._state["requested_quantity"] == 55
    assert any(event.kind == "spy_option_entry" for event in summary.events)


def test_spy_options_loop_caps_entry_by_visible_ask_contracts(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    broker = _Broker(_option_quote(ask_size="7"))
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options", spy_option_initial_budget_usd=10000),
        state_file=tmp_path / "spy_options_state.json", now_provider=lambda: now,
    )
    loop._evaluate_signal = lambda: OptionSignal("CALL", "strong_bullish", "test", .82, .30, 4, "up")

    loop.run_once()

    assert broker.orders[0][1] == 7


def test_weak_signal_uses_relaxed_contract_fallback_when_strict_selector_rejects(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    option_symbol = "SPY261028P00600000"
    broker = _Broker(_option_quote(
        symbol=option_symbol, bid="0.60", ask="0.90", delta="-0.30",
        gamma=None, theta=None, vega=None, rho=None, imp_vol=None, volume="0",
    ))
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json", now_provider=lambda: now,
    )
    loop._evaluate_signal = lambda: OptionSignal(
        "PUT", "weak_bullish_fade", "weak fallback", .61, .34, 2, "up",
    )
    loop._list_contracts = lambda *args: [
        OptionContract(option_symbol, now.date().isoformat(), Decimal("600"), "PUT"),
    ]

    summary = loop.run_once()

    assert broker.orders[0][2] == "BUY_TO_OPEN"
    assert loop._state["weak_fallback_used"] is True
    assert any(event.kind == "spy_option_weak_fallback" for event in summary.events)


def test_strong_signal_does_not_use_weak_contract_fallback(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    broker = _Broker(_option_quote(bid="0.60", ask="0.90", rho=None, imp_vol=None))
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json", now_provider=lambda: now,
    )
    loop._evaluate_signal = lambda: OptionSignal(
        "CALL", "strong_bullish", "strong reading", .82, .30, 4, "up",
    )

    summary = loop.run_once()

    assert broker.orders == []
    assert not any(event.kind == "spy_option_weak_fallback" for event in summary.events)


@pytest.mark.parametrize("bid, reason", [("1.17", "TAKE_PROFIT"), ("0.81", "STOP_LOSS")])
def test_take_profit_and_stop_loss_use_option_premium(tmp_path, bid, reason):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    broker = _Broker(_option_quote(bid=bid, ask="1.20"))
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json", now_provider=lambda: now,
    )
    loop._state.update({
        "session_date": now.date().isoformat(), "status": "OPEN",
        "option_symbol": "SPY261028C00600000", "option_type": "CALL",
        "strike": "600", "expiration": now.date().isoformat(),
        "entry_average_price": "0.90", "entry_filled_quantity": 1,
        "exit_filled_quantity": 0,
    })

    loop.run_once()

    assert loop._state["exit_reason"] == reason
    assert broker.orders[-1][2] == "SELL_TO_CLOSE"


def test_partial_exit_notional_uses_cumulative_average_fill(tmp_path):
    loop = SPY0DTEOptionsLoop(
        _Broker(_option_quote()), BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json",
        now_provider=lambda: datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN),
    )
    loop._state.update({"exit_notional": "0", "exit_filled_quantity": 0, "pending_order_filled_seen": 0})

    loop._record_exit_fill({"filled_quantity": "1", "filled_avg_price": "1.00"})
    loop._record_exit_fill({"filled_quantity": "2", "filled_avg_price": "1.25"})

    assert loop._state["exit_filled_quantity"] == 2
    assert Decimal(loop._state["exit_notional"]) == Decimal("250.00")


def test_unpriced_partial_exit_does_not_advance_reconciled_quantity(tmp_path):
    loop = SPY0DTEOptionsLoop(
        _Broker(_option_quote()), BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json",
        now_provider=lambda: datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN),
    )
    loop._state.update({"exit_notional": "0", "exit_filled_quantity": 0, "pending_order_filled_seen": 0})

    loop._record_exit_fill({"filled_quantity": "1"})

    assert loop._state["exit_filled_quantity"] == 0
    assert loop._state["pending_order_filled_seen"] == 0


def test_realized_pnl_compounds_budget_and_persists_five_trade_adjustment(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    performance_file = tmp_path / "spy_options_performance.json"
    loop = SPY0DTEOptionsLoop(
        _Broker(_option_quote()), BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json", performance_file=performance_file,
        now_provider=lambda: now,
    )
    loop._performance.update({
        "strategy_equity_usd": "100.00",
        "trades": [{"outcome": result} for result in ("win", "loss", "loss", "loss")],
    })
    loop._state.update({
        "session_date": now.date().isoformat(), "option_symbol": "SPY261028C00600000",
        "expiration": now.date().isoformat(), "option_type": "CALL", "signal_regime": "strong_bullish",
        "entry_average_price": "0.90", "exit_filled_quantity": 1, "exit_notional": "80.00",
    })

    summary = LoopSummary()
    loop._log_realized_pnl(summary)
    restored = SPY0DTEOptionsLoop(
        _Broker(_option_quote()), BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "restored_state.json", performance_file=performance_file,
        now_provider=lambda: now,
    )

    assert restored._performance["strategy_equity_usd"] == "90.00"
    assert restored._performance["reviews"][-1]["wins"] == 1
    assert restored._effective_config().spy_option_strong_score == pytest.approx(0.73)
    assert restored._effective_config().spy_option_weak_floor == pytest.approx(0.58)
    assert any(event.kind == "spy_options_performance_review" for event in summary.events)


def test_corrupt_persistent_performance_equity_blocks_startup(tmp_path):
    performance_file = tmp_path / "spy_options_performance.json"
    performance_file.write_text('{"strategy_equity_usd":"not-money"}', encoding="utf-8")

    with pytest.raises(RuntimeError, match="equity invalid"):
        SPY0DTEOptionsLoop(
            _Broker(_option_quote()), BotConfig(strategy_mode="spy_0dte_options"),
            state_file=tmp_path / "spy_options_state.json", performance_file=performance_file,
            now_provider=lambda: datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN),
        )


def test_performance_pause_blocks_new_entries(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    performance_file = tmp_path / "spy_options_performance.json"
    performance_file.write_text(
        '{"paused":true,"pause_reason":"manual review required"}', encoding="utf-8",
    )
    broker = _Broker(_option_quote())
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json", performance_file=performance_file,
        now_provider=lambda: now,
    )
    loop._evaluate_signal = lambda: pytest.fail("paused loop should not evaluate an entry signal")

    summary = loop.run_once()

    assert broker.orders == []
    assert any(event.kind == "spy_options_paused" for event in summary.events)


def test_same_day_inactive_state_with_account_position_blocks_startup(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    state_file = tmp_path / "spy_options_state.json"
    state_file.write_text(
        '{"session_date":"2026-10-28","status":"CLOSED"}', encoding="utf-8",
    )
    broker = _Broker(_option_quote())
    broker.get_positions = lambda: {"SPY": SimpleNamespace(quantity=1)}

    with pytest.raises(RuntimeError, match="conflict with inactive"):
        SPY0DTEOptionsLoop(
            broker, BotConfig(strategy_mode="spy_0dte_options"), state_file=state_file,
            now_provider=lambda: now,
        )


def test_stale_pending_order_requires_manual_reconciliation(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    state_file = tmp_path / "spy_options_state.json"
    state_file.write_text(
        '{"session_date":"2026-10-27","status":"EXIT_PENDING","pending_order_id":"order-1",'
        '"pending_order_role":"EXIT"}',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="order is pending"):
        SPY0DTEOptionsLoop(
            _Broker(_option_quote()), BotConfig(strategy_mode="spy_0dte_options"),
            state_file=state_file, now_provider=lambda: now,
        )


def test_expired_open_position_is_written_off_in_performance_ledger(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    state_file = tmp_path / "spy_options_state.json"
    state_file.write_text(
        '{"session_date":"2026-10-27","status":"OPEN","option_symbol":"SPY261027C00600000",'
        '"option_type":"CALL","strike":"600","expiration":"2026-10-27",'
        '"entry_filled_quantity":1,"exit_filled_quantity":0,"entry_average_price":"0.90",'
        '"exit_notional":"0"}',
        encoding="utf-8",
    )
    performance_file = tmp_path / "spy_options_performance.json"

    loop = SPY0DTEOptionsLoop(
        _Broker(_option_quote()), BotConfig(strategy_mode="spy_0dte_options"),
        state_file=state_file, performance_file=performance_file, now_provider=lambda: now,
    )

    assert loop._state == {}
    assert loop._performance["strategy_equity_usd"] == "10.00"
    assert loop._performance["trades"][0]["outcome"] == "loss"
    assert loop._performance["trades"][0]["expiry_assumed_worthless_contracts"] == 1


def test_spy_options_loop_does_not_enter_without_snapshot_depth(tmp_path, monkeypatch):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    broker = _Broker(_option_quote(ask_size="0"))
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json", now_provider=lambda: now,
    )
    loop._evaluate_signal = lambda: OptionSignal("CALL", "strong_bullish", "test", .82, .30, 4, "up")

    summary = loop.run_once()

    assert broker.orders == []
    assert any(event.kind == "spy_option_rejected" for event in summary.events)


def test_expired_prior_day_state_resets_only_when_account_is_flat(tmp_path):
    state_file = tmp_path / "spy_options_state.json"
    state_file.write_text(
        '{"session_date":"2026-10-27","status":"CLOSED","entry_filled_quantity":1}',
        encoding="utf-8",
    )
    broker = _Broker(_option_quote())
    now = datetime(2026, 10, 28, 9, 25, tzinfo=EASTERN)
    loop = SPY0DTEOptionsLoop(broker, BotConfig(), state_file=state_file, now_provider=lambda: now)
    assert loop._state == {}

    broker.get_positions = lambda: {"SPY": SimpleNamespace(quantity=1)}
    with pytest.raises(RuntimeError, match="positions remain"):
        SPY0DTEOptionsLoop(broker, BotConfig(), state_file=state_file, now_provider=lambda: now)
