from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from god_help_tradebot import BotConfig, Quote
from god_help_tradebot.paper import OrderSide, OrderType, PaperOrder
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.spy_options import OptionSignal, SPY0DTEOptionsLoop


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
        broker, BotConfig(strategy_mode="spy_0dte_options"),
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
    assert broker.orders[0][1] == 108  # floor(10000 * .98 / (.90 * 100))
    assert broker.orders[0][2] == "BUY_TO_OPEN"
    assert any(event.kind == "spy_option_entry" for event in summary.events)
    assert loop._state["status"] == "ENTRY_PENDING"


def test_spy_options_loop_retries_quantity_rejection_at_half_size(tmp_path, monkeypatch):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    broker = _Broker(_option_quote())
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options"),
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

    assert requested_quantities == [108, 54]
    assert broker.orders[0][1] == 54
    assert loop._state["requested_quantity"] == 54
    assert any(event.kind == "spy_option_entry" for event in summary.events)


def test_spy_options_loop_caps_entry_by_visible_ask_contracts(tmp_path):
    now = datetime(2026, 10, 28, 10, 0, tzinfo=EASTERN)
    broker = _Broker(_option_quote(ask_size="7"))
    loop = SPY0DTEOptionsLoop(
        broker, BotConfig(strategy_mode="spy_0dte_options"),
        state_file=tmp_path / "spy_options_state.json", now_provider=lambda: now,
    )
    loop._evaluate_signal = lambda: OptionSignal("CALL", "strong_bullish", "test", .82, .30, 4, "up")

    loop.run_once()

    assert broker.orders[0][1] == 7


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
        '{"session_date":"2026-10-27","status":"OPEN","entry_filled_quantity":1}',
        encoding="utf-8",
    )
    broker = _Broker(_option_quote())
    now = datetime(2026, 10, 28, 9, 25, tzinfo=EASTERN)
    loop = SPY0DTEOptionsLoop(broker, BotConfig(), state_file=state_file, now_provider=lambda: now)
    assert loop._state == {}

    broker.get_positions = lambda: {"SPY": SimpleNamespace(quantity=1)}
    with pytest.raises(RuntimeError, match="positions remain"):
        SPY0DTEOptionsLoop(broker, BotConfig(), state_file=state_file, now_provider=lambda: now)
