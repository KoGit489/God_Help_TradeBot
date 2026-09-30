from decimal import Decimal

from god_help_tradebot import OrderSide, OrderStatus, OrderType, PaperBroker, Quote


def quote(bid: float, ask: float, last: float) -> Quote:
    return Quote("TEST", Decimal(str(bid)), Decimal(str(ask)), Decimal(str(last)))


def test_market_entry_creates_virtual_position() -> None:
    broker = PaperBroker(1_000)
    order = broker.submit_order("TEST", OrderSide.BUY, 10, OrderType.MARKET)

    filled = broker.process_quote(quote(2.00, 2.01, 2.005))

    assert filled == [order]
    assert order.status is OrderStatus.FILLED
    assert order.fill_price == Decimal("2.01")
    assert broker.positions["TEST"].quantity == 10
    assert broker.cash == Decimal("979.90")


def test_target_fill_cancels_stop_and_records_pnl() -> None:
    broker = PaperBroker(1_000)
    broker.submit_order("TEST", OrderSide.BUY, 10, OrderType.MARKET)
    broker.process_quote(quote(2.00, 2.01, 2.005))
    stop, target = broker.submit_long_bracket("TEST", 10, 1.90, 2.30)

    filled = broker.process_quote(quote(2.30, 2.31, 2.30))

    assert filled == [target]
    assert target.status is OrderStatus.FILLED
    assert stop.status is OrderStatus.CANCELED
    assert broker.positions["TEST"].quantity == 0
    assert broker.realized_pnl == Decimal("2.90")


def test_stop_fill_cancels_target() -> None:
    broker = PaperBroker(1_000)
    broker.submit_order("TEST", OrderSide.BUY, 10, OrderType.MARKET)
    broker.process_quote(quote(2.00, 2.01, 2.005))
    stop, target = broker.submit_long_bracket("TEST", 10, 1.90, 2.30)

    filled = broker.process_quote(quote(1.89, 1.91, 1.89))

    assert filled == [stop]
    assert stop.status is OrderStatus.FILLED
    assert target.status is OrderStatus.CANCELED
    assert broker.realized_pnl == Decimal("-1.20")