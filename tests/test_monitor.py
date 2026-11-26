from datetime import datetime, time
from decimal import Decimal

import pytest

from god_help_tradebot import (
    BotConfig,
    ExitPlan,
    MonitorLoop,
    OrderStatus,
    OrderType,
    OrderSide,
    PaperBroker,
    Position,
    Quote,
)
from god_help_tradebot.monitor import LoopSummary
from god_help_tradebot.screen import MarketSnapshot
from god_help_tradebot.schedule import EASTERN


def _open_market_now() -> datetime:
    # A mid-session Wednesday keeps get_trading_session inside market hours.
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _closed_market_now() -> datetime:
    return datetime(2026, 10, 7, 18, 0, tzinfo=EASTERN)


def _night_session_now() -> datetime:
    # 9 PM ET is inside Webull's night session (8 PM - 4 AM), when trading is allowed.
    return datetime(2026, 10, 7, 21, 0, tzinfo=EASTERN)


def _snapshot(symbol: str, price: float, day_low: float, day_high: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        price=price,
        change_percent=5.0,
        volume=500_000,
        average_volume=100_000,
        bid=price - 0.01,
        ask=price + 0.01,
        day_low=day_low,
        day_high=day_high,
    )


def _broker_with_position(symbol: str, quantity: int, avg: float, quote: Quote) -> PaperBroker:
    broker = PaperBroker(10_000)
    broker.positions[symbol] = Position(quantity=quantity, average_price=Decimal(str(avg)))
    broker.last_quotes[symbol] = quote
    return broker


def test_rate_limit_uses_cooldown_without_counting_toward_halt():
    loop = MonitorLoop(PaperBroker(1_000), now_provider=_open_market_now)

    class RateLimitError(RuntimeError):
        http_status = 429

    def fail(summary):
        raise RateLimitError("TOO_MANY_REQUESTS")

    loop._run_cycle = fail
    result = loop.run_once()
    assert loop._consecutive_errors == 0
    assert loop._rate_limit_backoff_active is True
    assert loop.error_backoff_seconds == 300
    assert result.events[-1].kind == "rate_limit_backoff"

    loop._run_cycle = lambda summary: setattr(summary, "polls", summary.polls + 1)
    loop.run_once()
    assert loop._rate_limit_backoff_active is False
    assert loop.error_backoff_seconds == 30


def test_account_outage_retries_with_reconciliation_before_polling():
    class UnavailableBroker(PaperBroker):
        def __init__(self):
            super().__init__(1_000)
            self.calls = []
            self.fail_positions = True
            self.fail_orders = True

        def get_positions(self):
            self.calls.append("positions")
            if self.fail_positions:
                raise RuntimeError("HTTP Status: 503, Code: SERVICE_NOT_AVAILABLE")
            return super().get_positions()

        def assert_no_remote_open_orders(self):
            self.calls.append("orders")
            if self.fail_orders:
                raise RuntimeError("HTTP 503")

    broker = UnavailableBroker()
    loop = MonitorLoop(broker, now_provider=_open_market_now, flatten_before_close=False)
    summary = loop.run_once()
    assert loop._account_recovery_required
    assert loop._consecutive_errors == 0
    assert loop.error_backoff_seconds == 30
    assert broker.calls == ["positions"]

    for delay in (60, 120, 240, 300, 300):
        loop.run_once(summary)
        assert loop.error_backoff_seconds == delay
        assert loop._consecutive_errors == 0
    assert broker.calls == ["positions"] + ["orders"] * 5

    broker.fail_orders = False
    loop.run_once(summary)
    assert broker.calls[-2:] == ["orders", "positions"]
    assert loop._account_recovery_required
    broker.fail_positions = False
    loop.run_once(summary)
    assert broker.calls[-3:] == ["orders", "positions", "positions"]
    assert not loop._account_recovery_required
    assert not loop._service_unavailable_backoff_active
    assert loop.error_backoff_seconds == 30
    assert any(event.kind == "account_recovered" for event in summary.events)


def test_unexpected_remote_order_keeps_outage_recovery_locked():
    class UnexpectedOrderBroker(PaperBroker):
        def assert_no_remote_open_orders(self):
            raise RuntimeError("unexpected remote order")

    loop = MonitorLoop(UnexpectedOrderBroker(1_000))
    loop._account_recovery_required = True
    loop._run_cycle = lambda summary: pytest.fail("must not poll before reconciliation")
    result = loop.run_once()
    assert loop._account_recovery_required
    assert loop._consecutive_errors == 1
    assert result.events[-1].kind == "error"


def test_outage_recovery_allows_only_ledger_tracked_remote_orders():
    from types import SimpleNamespace

    class TrackedOrderBroker(PaperBroker):
        def assert_no_remote_open_orders(self, *, allowed_client_order_ids):
            assert allowed_client_order_ids == frozenset({"tracked-order"})

    loop = MonitorLoop(TrackedOrderBroker(1_000))
    loop.performance_store = SimpleNamespace(recovery_ids=frozenset({"tracked-order"}))
    loop._account_recovery_required = True
    loop._run_cycle = lambda summary: setattr(summary, "polls", summary.polls + 1)
    result = loop.run_once()
    assert not loop._account_recovery_required
    assert result.events[-1].kind == "account_recovered"


def test_order_operation_503_is_not_treated_as_safe_account_read_retry():
    loop = MonitorLoop(PaperBroker(1_000))
    loop._run_cycle = lambda summary: (_ for _ in ()).throw(RuntimeError("HTTP 503"))
    result = loop.run_once()
    assert loop._consecutive_errors == 1
    assert not loop._service_unavailable_backoff_active
    assert result.events[-1].kind == "error"


def test_monitor_exits_at_target_price() -> None:
    quote = Quote("NIVF", Decimal("0.15"), Decimal("0.16"), Decimal("0.15"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    plan = ExitPlan(symbol="NIVF", quantity=100, target_price=Decimal("0.15"), stop_price=Decimal("0.09"))
    loop = MonitorLoop(broker, exit_plans={"NIVF": plan}, now_provider=_open_market_now, flatten_before_close=False)

    summary = loop.run_once()

    assert summary.exits[0].order_type is OrderType.MARKET
    assert summary.exits[0].status is OrderStatus.OPEN
    assert summary.events[0].kind == "take_profit_order"


def test_monitor_exits_at_stop_price() -> None:
    quote = Quote("NIVF", Decimal("0.08"), Decimal("0.09"), Decimal("0.08"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    plan = ExitPlan(symbol="NIVF", quantity=100, target_price=Decimal("0.15"), stop_price=Decimal("0.09"))
    loop = MonitorLoop(broker, exit_plans={"NIVF": plan}, now_provider=_open_market_now, flatten_before_close=False)

    summary = loop.run_once()

    assert summary.exits[0].order_type is OrderType.MARKET  # breached stop exits immediately
    assert summary.exits[0].status is OrderStatus.OPEN
    assert summary.events[0].kind == "stop_loss_order"


def test_monitor_holds_within_bracket() -> None:
    quote = Quote("NIVF", Decimal("0.11"), Decimal("0.12"), Decimal("0.11"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    plan = ExitPlan(symbol="NIVF", quantity=100, target_price=Decimal("0.15"), stop_price=Decimal("0.09"))
    loop = MonitorLoop(broker, exit_plans={"NIVF": plan}, now_provider=_night_session_now, flatten_before_close=False)

    summary = loop.run_once()

    assert summary.exits == []
    assert summary.events == []


def test_monitor_skips_position_without_exit_plan() -> None:
    quote = Quote("NIVF", Decimal("0.08"), Decimal("0.09"), Decimal("0.08"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    loop = MonitorLoop(broker, now_provider=_night_session_now, flatten_before_close=False)

    summary = loop.run_once()

    assert summary.exits == []
    assert summary.positions_seen["NIVF"].quantity == 100


def test_monitor_enters_top_candidate_when_market_open() -> None:
    broker = PaperBroker(10_000)
    config = BotConfig(max_symbol_price=10.0, risk_per_trade_usd=25.0, max_position_value_usd=500.0)
    loop = MonitorLoop(
        broker,
        config,
        snapshot_provider=lambda: [_snapshot("GOW", 3.00, 2.80, 3.20)],
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert len(summary.entries) == 1
    assert summary.events[0].kind == "entry"
    assert "GOW" in loop.exit_plans
    assert summary.entries[0].side is OrderSide.BUY


def test_monitor_does_not_enter_when_market_closed() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [_snapshot("GOW", 3.00, 2.80, 3.20)],
        now_provider=_closed_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries == []
    assert loop.exit_plans == {}


def test_monitor_does_not_enter_while_holding() -> None:
    quote = Quote("NIVF", Decimal("0.11"), Decimal("0.12"), Decimal("0.11"))
    broker = _broker_with_position("NIVF", 100, 0.10, quote)
    plan = ExitPlan(symbol="NIVF", quantity=100, target_price=Decimal("0.15"), stop_price=Decimal("0.09"))
    loop = MonitorLoop(
        broker,
        exit_plans={"NIVF": plan},
        snapshot_provider=lambda: [_snapshot("GOW", 3.00, 2.80, 3.20)],
        now_provider=_open_market_now,
        allow_entries=True,
    )

    summary = loop.run_once()

    assert summary.entries == []


def test_monitor_run_respects_max_polls() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(broker, now_provider=_closed_market_now, poll_seconds=0, max_polls=3)

    summary = loop.run()

    assert summary.polls == 3


def test_monitor_run_once_returns_cumulative_summary() -> None:
    broker = PaperBroker(10_000)
    loop = MonitorLoop(broker, now_provider=_night_session_now, flatten_before_close=False)
    summary = LoopSummary()

    loop.run_once(summary)
    loop.run_once(summary)

    assert summary.polls == 2
