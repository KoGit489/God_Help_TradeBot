"""Trade outcome logging: fill prices, P&L, and fill-anchored brackets."""

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from god_help_tradebot import BotConfig, MonitorLoop, PaperBroker, Quote
from god_help_tradebot.monitor import ExitPlan
from god_help_tradebot.paper import OrderSide, OrderStatus, OrderType, PaperOrder, Position
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.screen import MarketSnapshot
from god_help_tradebot.state import EquityPerformanceStore
from god_help_tradebot.broker import WebullSandboxBroker


def test_four_of_five_keeps_picking_gates_and_reviews_each_batch_once(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    ledger.data["trades"] = [{"outcome": "win" if number < 4 else "loss"} for number in range(5)]
    _, reviews = ledger.finalize({}, _open_now(), BotConfig(), 0.5)
    assert reviews[0]["wins"] == 4
    assert reviews[0]["changes"] == {}
    assert ledger.finalize({}, _open_now(), BotConfig(), 0.5)[1] == []
    ledger.data["trades"].extend([{"outcome": "flat"}] * 5)
    _, reviews = ledger.finalize({}, _open_now(), BotConfig(), 0.5)
    assert reviews[0]["through_trade"] == 10
    assert reviews[0]["wins"] == 0


def test_missed_trade_target_at_tightening_limits_pauses_new_entries(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    ledger.data["trades"] = [{"outcome": "loss"}] * 5
    config = BotConfig(min_relative_volume=5, min_signals_present=4, max_spread_percent=0.3)
    ledger.finalize({}, _open_now(), config, 0.90)
    assert ledger.data["paused"] is True


def test_unpriced_fill_and_account_mismatch_require_reconciliation(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    order = PaperOrder(1, "TEST", OrderSide.BUY, 10, OrderType.MARKET,
                       filled_quantity=5, client_order_id="entry")
    ledger.record_fill(order, "ENTRY", _open_now())
    assert ledger.data["paused"] is True
    assert ledger.data["trades"] == []
    with pytest.raises(RuntimeError, match="another account"):
        EquityPerformanceStore(ledger.path, "different-account")


def test_larger_partial_fill_without_average_does_not_reuse_old_price(tmp_path):
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    order = PaperOrder(1, "TEST", OrderSide.BUY, 100, OrderType.MARKET, client_order_id="entry")
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    ledger.track_order(order, "ENTRY")
    broker.apply_order_status(order, {"status": "PARTIAL_FILLED", "filled_quantity": "30", "filled_avg_price": "10"})
    ledger.record_fill(order, "ENTRY", _open_now())
    broker.apply_order_status(order, {"status": "FILLED", "filled_quantity": "100"})
    assert order.fill_price is None
    ledger.record_fill(order, "ENTRY", _open_now())
    assert ledger.data["paused"] is True
    assert ledger.data["active"]["TEST"]["entry_quantity"] == 30


def test_entry_position_reporting_lag_is_not_a_completed_trade(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    ledger.record_fill(PaperOrder(1, "TEST", OrderSide.BUY, 10, OrderType.MARKET,
                                 status=OrderStatus.FILLED, fill_price=Decimal("10"), client_order_id="entry"), "ENTRY", _open_now())
    assert ledger.finalize({}, _open_now(), BotConfig(), 0.5)[0] == []
    assert not ledger.data["paused"]


def test_performance_ledger_is_created_at_startup_and_corruption_blocks_load(tmp_path):
    path = tmp_path / "performance.json"
    ledger = EquityPerformanceStore(path, "test-account")
    assert path.exists()
    assert ledger.data["trades"] == []
    path.write_text("invalid json", encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreadable"):
        EquityPerformanceStore(path, "test-account")
    assert path.read_text(encoding="utf-8") == "invalid json"


def test_pending_exit_recovers_cumulative_fill_after_restart(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    entry = PaperOrder(1, "TEST", OrderSide.BUY, 100, OrderType.MARKET,
                       status=OrderStatus.FILLED, fill_price=Decimal("10"), client_order_id="entry")
    ledger.record_fill(entry, "ENTRY", _open_now())
    exit_order = PaperOrder(2, "TEST", OrderSide.SELL, 100, OrderType.MARKET,
                            filled_quantity=30, fill_price=Decimal("11"), client_order_id="exit")
    ledger.track_order(exit_order, "EXIT")
    ledger.record_fill(exit_order, "EXIT", _open_now())
    ledger = EquityPerformanceStore(ledger.path, "test-account")
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    broker.session = SimpleNamespace()
    broker._orders = {}
    broker._next_order_id = 1
    broker.get_order_status = lambda client_id: {"status": "FILLED", "filled_quantity": "100", "filled_avg_price": "11"}
    broker.get_positions = lambda: {}
    loop = MonitorLoop(broker, performance_store=ledger, now_provider=_open_now, flatten_before_close=False)
    result = loop.run_once()
    assert not any(event.kind == "error" for event in result.events)
    assert len(ledger.data["trades"]) == 1
    assert ledger.data["trades"][0]["realized_pnl_usd"] == "100.00"
    assert ledger.recovery_ids == frozenset()


def test_review_pause_does_not_disable_existing_position_exit(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    ledger.record_fill(PaperOrder(1, "TEST", OrderSide.BUY, 10, OrderType.MARKET,
                                 status=OrderStatus.FILLED, fill_price=Decimal("10"), client_order_id="entry"), "ENTRY", _open_now())
    ledger.data["paused"] = True
    broker = PaperBroker(1_000)
    broker.positions["TEST"] = Position(10, Decimal("10"))
    broker.last_quotes["TEST"] = Quote("TEST", Decimal("13"), Decimal("13.01"), Decimal("13"))
    loop = MonitorLoop(broker, performance_store=ledger, now_provider=_open_now,
                       allow_entries=True, flatten_before_close=False)
    result = loop.run_once()
    assert len(result.exits) == 1
    assert not result.entries


def test_fill_ledger_counts_one_trade_after_split_exit_and_survives_restart(tmp_path):
    path = tmp_path / "performance.json"
    ledger = EquityPerformanceStore(path, "test-account")
    now = _open_now()
    entry = PaperOrder(1, "TEST", OrderSide.BUY, 100, OrderType.MARKET,
                       status=OrderStatus.FILLED, fill_price=Decimal("10"), client_order_id="entry")
    ledger.record_fill(entry, "ENTRY", now)
    first = PaperOrder(2, "TEST", OrderSide.SELL, 40, OrderType.MARKET,
                       status=OrderStatus.FILLED, fill_price=Decimal("11"), client_order_id="exit-1")
    ledger.record_fill(first, "EXIT", now)
    assert ledger.finalize({"TEST": Position(60, Decimal("10"))}, now, BotConfig(), 0.5)[0] == []
    ledger = EquityPerformanceStore(path, "test-account")
    ledger.record_fill(first, "EXIT", now)
    last = PaperOrder(3, "TEST", OrderSide.SELL, 60, OrderType.MARKET,
                      status=OrderStatus.FILLED, fill_price=Decimal("9"), client_order_id="exit-2")
    ledger.record_fill(last, "EXIT", now)
    completed, _ = ledger.finalize({}, now, BotConfig(), 0.5)
    assert len(completed) == 1
    assert completed[0]["realized_pnl_usd"] == "-20.00"
    assert ledger.finalize({}, now, BotConfig(), 0.5)[0] == []


def test_five_completed_trades_below_four_wins_tighten_picking(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    now = _open_now()
    for number in range(5):
        entry = PaperOrder(number * 2, "TEST", OrderSide.SHORT, 10, OrderType.MARKET,
                           status=OrderStatus.FILLED, fill_price=Decimal("10"), client_order_id=f"entry-{number}")
        exit_order = PaperOrder(number * 2 + 1, "TEST", OrderSide.BUY, 10, OrderType.MARKET,
                                status=OrderStatus.FILLED, fill_price=Decimal("9" if number < 3 else "11"), client_order_id=f"exit-{number}")
        ledger.record_fill(entry, "ENTRY", now)
        ledger.record_fill(exit_order, "EXIT", now)
        _, reviews = ledger.finalize({}, now, BotConfig(), 0.5)
        assert len(reviews) == (1 if number == 4 else 0)
    assert reviews[0]["wins"] == 3
    assert ledger.effective_config(BotConfig()).min_relative_volume == 2.25
    assert ledger.data["adaptive"]["min_confirmation_score"] == 0.53
    ledger = EquityPerformanceStore(ledger.path, "test-account")
    assert len(ledger.data["trades"]) == 5
    assert ledger.data["reviewed_count"] == 5


def test_take_profit_sessions_bootstrap_from_fills_and_keep_win_review_independent(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    config = BotConfig(account_price_priority=True)
    monday = datetime(2026, 10, 5, 11, tzinfo=EASTERN)
    for index, price in enumerate(("10.252", "13", "13", "12.999", "13")):
        now = monday + timedelta(days=index)
        for trade_number in range(2 if index == 0 else 1):
            suffix = f"{index}-{trade_number}"
            for side, role, fill in ((OrderSide.BUY, "ENTRY", "10"), (OrderSide.SELL, "EXIT", price)):
                ledger.record_fill(PaperOrder(1, "MARA", side, 10, OrderType.MARKET,
                                              status=OrderStatus.FILLED, fill_price=Decimal(fill),
                                              client_order_id=f"{role}-{suffix}"), role, now)
            ledger.finalize({}, now, config, 0.5)
        assert ledger.review_take_profit(now, observe=True) == []
        session_results = ledger.review_take_profit(now.replace(hour=16), observe=True)
        assert len(session_results) == 1
        assert session_results[0][1:] == (price == "13", (0, 1, 2, 2, 3)[index], index + 1)
    ledger = EquityPerformanceStore(ledger.path, "test-account")
    assert ledger.prioritize_prepop
    assert ledger.review_take_profit(now, observe=True) == []
    assert ledger.data["reviews"][0]["wins"] == 5
    assert ledger.data["adaptive"] == {}
    assert not ledger.data["paused"]
    assert ledger.effective_config(config) == config


def test_take_profit_counts_only_observed_sessions_or_verified_trade_days(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    now = _open_now()
    ledger.data["trades"].append({
        "closed_at": (now - timedelta(days=2)).isoformat(),
        "entry_notional": "100", "exit_notional": "102.52", "direction": "long",
    })
    assert ledger.review_take_profit(now, observe=True) == [("2026-10-05", False, 0, 1)]
    assert ledger.review_take_profit(now.replace(hour=16), observe=True) == [("2026-10-07", False, 0, 2)]
    assert not ledger.prioritize_prepop


def test_take_profit_prepop_preference_preserves_win_review_gates(tmp_path):
    ledger = EquityPerformanceStore(tmp_path / "performance.json", "test-account")
    config = BotConfig(account_price_priority=True)
    loop = MonitorLoop(PaperBroker(1_000), config, performance_store=ledger)
    candidates = [_snap("MOMENTUM", 10, 9), _snap("COIL", 3, 1)]
    assert loop._ranked_profiles(candidates)[0].candidate.snapshot.symbol == "MOMENTUM"
    ledger.data["take_profit_sessions"] = {f"2026-10-0{day}": False for day in range(5, 10)}
    assert loop._ranked_profiles(candidates)[0].candidate.snapshot.symbol == "COIL"
    assert loop.config == config


def _open_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _snap(symbol: str = "GOW", price: float = 3.0, change: float = 5.0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol, price, change, 500_000, 100_000,
        price - 0.01, price + 0.01, price * 0.9, price * 1.1,
    )


def _quote(symbol: str, bid: str, ask: str) -> Quote:
    return Quote(symbol, Decimal(bid), Decimal(ask), Decimal(bid))


def _entered_loop() -> tuple[MonitorLoop, PaperBroker]:
    """Loop with a resting patient-limit entry at 2.995 on GOW."""
    broker = PaperBroker(10_000)
    broker.last_quotes["GOW"] = _quote("GOW", "2.99", "3.01")
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snap()],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )
    loop.run_once()
    return loop, broker


def test_entry_fill_logs_price_and_reanchors_bracket() -> None:
    loop, broker = _entered_loop()
    plan = loop.exit_plans["GOW"]
    assert plan.target_price == Decimal("3.9")  # anchored to the 3.00 decision price

    # The market comes to us: ask drops to 2.95, filling the resting limit.
    broker.process_quote(_quote("GOW", "2.94", "2.95"))
    summary = loop.run_once()

    assert any(e.kind == "entry_filled" and "@ 2.95" in e.detail for e in summary.events)
    # Bracket re-anchored to the true 2.95 fill, not the 3.00 decision price.
    plan = loop.exit_plans["GOW"]
    assert plan.target_price == Decimal("2.95") * Decimal("1.3")
    assert plan.stop_price == Decimal("2.95") * Decimal("0.9")


def test_exit_fill_logs_winning_pnl() -> None:
    loop, broker = _entered_loop()
    broker.process_quote(_quote("GOW", "2.94", "2.95"))  # fills entry at 2.95
    loop.run_once()

    broker.process_quote(_quote("GOW", "3.84", "3.85"))  # bid over the 3.835 target
    loop.run_once()  # take-profit submitted
    broker.process_quote(_quote("GOW", "3.84", "3.85"))  # fills the exit
    summary = loop.run_once()

    filled = [e for e in summary.events if e.kind == "exit_filled"]
    assert filled, "expected an exit_filled event"
    assert "P&L +$" in filled[0].detail
    assert "[win]" in filled[0].detail
    assert "(entry 2.95)" in filled[0].detail


def test_exit_fill_logs_losing_pnl() -> None:
    loop, broker = _entered_loop()
    broker.process_quote(_quote("GOW", "2.94", "2.95"))  # fills entry at 2.95
    loop.run_once()

    broker.process_quote(_quote("GOW", "2.64", "2.65"))  # bid under the 2.655 stop
    loop.run_once()  # breached stop exits at market
    broker.process_quote(_quote("GOW", "2.64", "2.65"))  # fills the exit
    summary = loop.run_once()

    filled = [e for e in summary.events if e.kind == "exit_filled"]
    assert filled, "expected an exit_filled event"
    assert "P&L -$" in filled[0].detail
    assert "[loss]" in filled[0].detail


def test_exit_fill_without_known_entry_still_logs() -> None:
    # Position and plan predate a restart: no recorded entry fill, still logs.
    broker = PaperBroker(10_000)
    broker.positions["GOW"] = Position(quantity=100, average_price=Decimal("2.50"))
    broker.last_quotes["GOW"] = _quote("GOW", "3.26", "3.27")
    loop = MonitorLoop(
        broker,
        BotConfig(allow_fallback=False),
        exit_plans={"GOW": ExitPlan("GOW", 100, Decimal("3.25"), Decimal("2.25"))},
        now_provider=_open_now,
        flatten_before_close=False,
    )

    loop.run_once()  # take-profit submitted (bid 3.26 >= 3.25 target)
    broker.process_quote(_quote("GOW", "3.26", "3.27"))  # fills at the bid
    summary = loop.run_once()

    filled = [e for e in summary.events if e.kind == "exit_filled"]
    assert filled, "expected an exit_filled event"
    assert "entry price unknown" in filled[0].detail
    assert "@ 3.26" in filled[0].detail


def test_partial_exit_fills_keep_entry_reference_for_pnl() -> None:
    loop = MonitorLoop(PaperBroker(10_000), now_provider=_open_now, flatten_before_close=False)
    loop._entry_fills["GOW"] = Decimal("2.50")
    from god_help_tradebot.monitor import LoopSummary

    result = LoopSummary()
    for order_id, fill_price in ((1, "2.40"), (2, "2.35")):
        order = PaperOrder(
            order_id=order_id,
            symbol="GOW",
            side=OrderSide.SELL,
            quantity=50,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            fill_price=Decimal(fill_price),
        )
        loop._on_exit_filled(order, result)

    exit_events = [event for event in result.events if event.kind == "exit_filled"]
    assert len(exit_events) == 2
    assert all("(entry 2.50)" in event.detail for event in exit_events)
    assert "P&L -$5.00" in exit_events[0].detail
    assert "P&L -$7.50" in exit_events[1].detail
