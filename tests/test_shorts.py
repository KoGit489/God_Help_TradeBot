from datetime import datetime
from decimal import Decimal

from god_help_tradebot import BotConfig, ExitPlan, MonitorLoop, OrderSide, OrderStatus, OrderType, PaperBroker, Position, Quote
from god_help_tradebot.risk import build_short_plan
from god_help_tradebot.schedule import EASTERN
from god_help_tradebot.screen import MarketSnapshot, screen_fade_candidates


def _open_now() -> datetime:
    return datetime(2026, 10, 7, 11, 0, tzinfo=EASTERN)


def _fade_snapshot(symbol: str, price: float, change: float, day_low: float, day_high: float) -> MarketSnapshot:
    return MarketSnapshot(
        symbol=symbol,
        price=price,
        change_percent=change,
        volume=500_000,
        average_volume=100_000,
        bid=price - 0.01,
        ask=price + 0.01,
        day_low=day_low,
        day_high=day_high,
    )


# --- PaperBroker short mechanics ---------------------------------------------

def test_paper_short_entry_and_cover_profit() -> None:
    broker = PaperBroker(10_000)
    broker.process_quote(Quote("GOW", Decimal("3.0"), Decimal("3.01"), Decimal("3.0")))
    broker.submit_order("GOW", OrderSide.SHORT, 100, OrderType.MARKET)
    broker.process_quote(Quote("GOW", Decimal("3.0"), Decimal("3.01"), Decimal("3.0")))

    position = broker.get_position("GOW")
    assert position.quantity == -100
    assert position.average_price == Decimal("3.0")

    # Price slides to 2.5: buy to cover at the ask for a profit.
    broker.submit_order("GOW", OrderSide.BUY, 100, OrderType.MARKET)
    broker.process_quote(Quote("GOW", Decimal("2.49"), Decimal("2.50"), Decimal("2.49")))

    assert broker.get_position("GOW").quantity == 0
    assert broker.realized_pnl == Decimal("50.00")


def test_paper_short_cover_loss() -> None:
    broker = PaperBroker(10_000)
    broker.submit_order("GOW", OrderSide.SHORT, 100, OrderType.MARKET)
    broker.process_quote(Quote("GOW", Decimal("3.0"), Decimal("3.01"), Decimal("3.0")))

    # Price rallies to 3.5: cover at a loss.
    broker.submit_order("GOW", OrderSide.BUY, 100, OrderType.MARKET)
    broker.process_quote(Quote("GOW", Decimal("3.49"), Decimal("3.50"), Decimal("3.49")))

    assert broker.realized_pnl == Decimal("-50.00")


# --- Short trade plan ----------------------------------------------------------

def test_short_plan_flips_stop_and_target() -> None:
    config = BotConfig(risk_per_trade_usd=25.0, reward_multiple=3.0, max_position_value_usd=500.0)
    plan = build_short_plan(3.0, 3.2, config)

    assert plan.stop_price == Decimal("3.2")
    assert plan.target_price == Decimal("2.4")
    assert plan.quantity == 125  # $25 risk / $0.20 per share
    assert plan.risk_usd == Decimal("25.00")


def test_short_plan_pct_mode() -> None:
    config = BotConfig(position_pct_of_account=1.0)
    plan = build_short_plan(2.0, 2.1, config, account_value=100_000)

    assert plan.quantity == 49_000  # 100k * 0.98 headroom / $2
    assert plan.risk_usd == Decimal("4900.00")


def test_short_plan_requires_stop_above_entry() -> None:
    try:
        build_short_plan(3.0, 2.8, BotConfig())
        raise AssertionError("should reject stop below entry")
    except ValueError:
        pass


# --- Fade screening --------------------------------------------------------------

def test_fade_screen_finds_faded_pumper() -> None:
    # Popped 40% but slid into the bottom of the day's range.
    snapshot = _fade_snapshot("BDAI", 2.85, 40.0, 2.80, 3.50)
    ranked = screen_fade_candidates([snapshot], BotConfig())

    assert len(ranked) == 1
    assert ranked[0].snapshot.symbol == "BDAI"
    assert ranked[0].score > 0


def test_fade_screen_rejects_small_moves() -> None:
    snapshot = _fade_snapshot("SLOW", 2.0, 5.0, 1.95, 2.10)
    assert screen_fade_candidates([snapshot], BotConfig()) == []


def test_fade_screen_rejects_still_near_highs() -> None:
    snapshot = _fade_snapshot("HOT", 3.45, 40.0, 2.80, 3.50)  # range_position ~0.93
    assert screen_fade_candidates([snapshot], BotConfig()) == []


# --- Monitor short flow -----------------------------------------------------------

def test_monitor_enters_short_on_fade_candidate() -> None:
    broker = PaperBroker(10_000)
    broker.last_quotes["BDAI"] = Quote("BDAI", Decimal("2.84"), Decimal("2.85"), Decimal("2.84"))
    snapshot = _fade_snapshot("BDAI", 2.85, 40.0, 2.80, 3.50)
    loop = MonitorLoop(
        broker,
        BotConfig(allow_shorts=True),
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].side is OrderSide.SHORT
    assert "[fade]" in summary.events[0].detail
    plan = loop.exit_plans["BDAI"]
    assert plan.direction == "short"
    assert plan.stop_price > plan.target_price


def test_monitor_ignores_fade_when_shorts_disabled() -> None:
    broker = PaperBroker(10_000)
    # Over 40% so the momentum profile rejects it; faded off the high.
    snapshot = _fade_snapshot("BDAI", 2.85, 45.0, 2.80, 3.50)
    loop = MonitorLoop(
        broker,
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert all(order.side is not OrderSide.SHORT for order in summary.entries)


def _short_broker(symbol: str, quantity: int, avg: float, quote: Quote) -> PaperBroker:
    broker = PaperBroker(10_000)
    broker.positions[symbol] = Position(quantity=-quantity, average_price=Decimal(str(avg)))
    broker.last_quotes[symbol] = quote
    return broker


def test_monitor_covers_short_at_target() -> None:
    quote = Quote("BDAI", Decimal("2.49"), Decimal("2.50"), Decimal("2.49"))
    broker = _short_broker("BDAI", 100, 3.0, quote)
    plan = ExitPlan("BDAI", 100, Decimal("2.50"), Decimal("3.20"), direction="short")
    loop = MonitorLoop(broker, exit_plans={"BDAI": plan}, now_provider=_open_now, flatten_before_close=False)

    summary = loop.run_once()

    assert summary.exits[0].side is OrderSide.BUY
    assert summary.events[0].kind == "take_profit_order"


def test_monitor_covers_short_at_stop() -> None:
    quote = Quote("BDAI", Decimal("3.20"), Decimal("3.21"), Decimal("3.20"))
    broker = _short_broker("BDAI", 100, 3.0, quote)
    plan = ExitPlan("BDAI", 100, Decimal("2.50"), Decimal("3.20"), direction="short")
    loop = MonitorLoop(broker, exit_plans={"BDAI": plan}, now_provider=_open_now, flatten_before_close=False)

    summary = loop.run_once()

    assert summary.exits[0].side is OrderSide.BUY
    assert summary.events[0].kind == "stop_loss_order"


def test_monitor_flatten_covers_short_position() -> None:
    quote = Quote("BDAI", Decimal("2.84"), Decimal("2.85"), Decimal("2.84"))
    broker = _short_broker("BDAI", 100, 3.0, quote)
    plan = ExitPlan("BDAI", 100, Decimal("2.50"), Decimal("3.20"), direction="short")
    loop = MonitorLoop(
        broker,
        exit_plans={"BDAI": plan},
        now_provider=lambda: datetime(2026, 10, 7, 15, 58, tzinfo=EASTERN),
        flatten_before_close=True,
    )

    summary = loop.run_once()

    assert summary.exits[0].side is OrderSide.BUY
    assert any(e.kind == "eod_flatten" for e in summary.events)


# --- Paradox mode: flip execution, not signals -----------------------------------

def test_paradox_mode_shorts_a_momentum_pick() -> None:
    broker = PaperBroker(10_000)
    broker.last_quotes["GOW"] = Quote("GOW", Decimal("2.99"), Decimal("3.01"), Decimal("3.0"))
    # A normal bullish momentum candidate (5% up, strong volume, near high).
    snapshot = _fade_snapshot("GOW", 3.00, 5.0, 2.80, 3.20)
    loop = MonitorLoop(
        broker,
        BotConfig(flip_entries=True),
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].side is OrderSide.SHORT
    assert "[momentum-flipped]" in summary.events[0].detail
    plan = loop.exit_plans["GOW"]
    assert plan.direction == "short"
    assert plan.stop_price > plan.target_price


def test_paradox_off_buys_momentum_pick() -> None:
    broker = PaperBroker(10_000)
    broker.last_quotes["GOW"] = Quote("GOW", Decimal("2.99"), Decimal("3.01"), Decimal("3.0"))
    snapshot = _fade_snapshot("GOW", 3.00, 5.0, 2.80, 3.20)
    loop = MonitorLoop(
        broker,
        BotConfig(),
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    summary = loop.run_once()

    assert summary.entries[0].side is OrderSide.BUY
    assert loop.exit_plans["GOW"].direction == "long"


def test_fixed_pct_bracket_on_long_entry() -> None:
    broker = PaperBroker(10_000)
    broker.last_quotes["GOW"] = Quote("GOW", Decimal("2.99"), Decimal("3.01"), Decimal("3.0"))
    snapshot = _fade_snapshot("GOW", 3.00, 5.0, 2.80, 3.20)
    loop = MonitorLoop(
        broker,
        BotConfig(),
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    loop.run_once()

    plan = loop.exit_plans["GOW"]
    assert plan.stop_price == Decimal("3.00") * Decimal("0.9")   # 10% below entry
    assert plan.target_price == Decimal("3.00") * Decimal("1.3")  # 30% above entry


def test_fixed_pct_bracket_on_short_entry() -> None:
    broker = PaperBroker(10_000)
    broker.last_quotes["GOW"] = Quote("GOW", Decimal("2.99"), Decimal("3.01"), Decimal("3.0"))
    snapshot = _fade_snapshot("GOW", 3.00, 5.0, 2.80, 3.20)
    loop = MonitorLoop(
        broker,
        BotConfig(flip_entries=True),
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    loop.run_once()

    plan = loop.exit_plans["GOW"]
    assert plan.direction == "short"
    assert plan.stop_price == Decimal("3.00") * Decimal("1.1")   # 10% above entry
    assert plan.target_price == Decimal("3.00") * Decimal("0.7")  # 30% below entry


def test_pct_bracket_is_configurable() -> None:
    broker = PaperBroker(10_000)
    broker.last_quotes["GOW"] = Quote("GOW", Decimal("2.99"), Decimal("3.01"), Decimal("3.0"))
    snapshot = _fade_snapshot("GOW", 3.00, 5.0, 2.80, 3.20)
    loop = MonitorLoop(
        broker,
        BotConfig(stop_pct=0.05, target_pct=0.15),
        snapshot_provider=lambda: [snapshot],
        now_provider=_open_now,
        allow_entries=True,
        flatten_before_close=False,
    )

    loop.run_once()

    plan = loop.exit_plans["GOW"]
    assert plan.stop_price == Decimal("3.00") * Decimal("0.95")
    assert plan.target_price == Decimal("3.00") * Decimal("1.15")
