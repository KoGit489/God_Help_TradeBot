from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(str, Enum):
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELED = "CANCELED"


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal


@dataclass
class PaperOrder:
    order_id: int
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    status: OrderStatus = OrderStatus.OPEN
    fill_price: Decimal | None = None
    client_order_id: str | None = None


@dataclass
class Position:
    quantity: int = 0
    average_price: Decimal = Decimal("0")


class PaperBroker:
    """Deterministic virtual broker for strategy tests; never sends live orders."""

    def __init__(self, starting_cash: float) -> None:
        self.cash = Decimal(str(starting_cash))
        if self.cash < 0:
            raise ValueError("starting_cash cannot be negative")
        self.realized_pnl = Decimal("0")
        self.positions: dict[str, Position] = {}
        self.orders: dict[int, PaperOrder] = {}
        self.last_quotes: dict[str, Quote] = {}
        self._next_order_id = 1

    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType,
        *,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trading_session: str = "CORE",
    ) -> PaperOrder:
        if not symbol.strip():
            raise ValueError("symbol is required")
        if quantity < 1:
            raise ValueError("quantity must be positive")
        if order_type is OrderType.LIMIT and limit_price is None:
            raise ValueError("limit_price is required for limit orders")
        if order_type is OrderType.STOP and stop_price is None:
            raise ValueError("stop_price is required for stop orders")

        order = PaperOrder(
            order_id=self._next_order_id,
            symbol=symbol.upper(),
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=self._decimal_or_none(limit_price),
            stop_price=self._decimal_or_none(stop_price),
        )
        self.orders[order.order_id] = order
        self._next_order_id += 1
        return order

    def submit_long_bracket(
        self,
        symbol: str,
        quantity: int,
        stop_price: float,
        target_price: float,
    ) -> tuple[PaperOrder, PaperOrder]:
        stop = self.submit_order(symbol, OrderSide.SELL, quantity, OrderType.STOP, stop_price=stop_price)
        target = self.submit_order(symbol, OrderSide.SELL, quantity, OrderType.LIMIT, limit_price=target_price)
        return stop, target

    def get_positions(self) -> dict[str, Position]:
        return dict(self.positions)

    def get_position(self, symbol: str) -> Position | None:
        return self.positions.get(symbol.upper())

    def get_latest_quote(self, symbol: str) -> Quote | None:
        return self.last_quotes.get(symbol.upper())

    def cancel_order(self, order_id: int) -> PaperOrder | None:
        order = self.orders.get(order_id)
        if order is None:
            return None
        if order.status is OrderStatus.OPEN:
            order.status = OrderStatus.CANCELED
        return order

    def process_quote(self, quote: Quote) -> list[PaperOrder]:
        self.last_quotes[quote.symbol.upper()] = quote
        filled: list[PaperOrder] = []
        for order in list(self.orders.values()):
            if order.status is not OrderStatus.OPEN or order.symbol != quote.symbol.upper():
                continue
            fill_price = self._fill_price(order, quote)
            if fill_price is None:
                continue
            self._apply_fill(order, fill_price)
            filled.append(order)
            if order.side is OrderSide.SELL:
                self._cancel_other_open_exits(order)
        return filled

    def _fill_price(self, order: PaperOrder, quote: Quote) -> Decimal | None:
        if order.order_type is OrderType.MARKET:
            return quote.ask if order.side is OrderSide.BUY else quote.bid
        if order.order_type is OrderType.LIMIT:
            if order.side is OrderSide.BUY and quote.ask <= order.limit_price:
                return quote.ask
            if order.side is OrderSide.SELL and quote.bid >= order.limit_price:
                return quote.bid
        if order.order_type is OrderType.STOP:
            if order.side is OrderSide.BUY and quote.last >= order.stop_price:
                return quote.ask
            if order.side is OrderSide.SELL and quote.last <= order.stop_price:
                return quote.bid
        return None

    def _apply_fill(self, order: PaperOrder, fill_price: Decimal) -> None:
        position = self.positions.setdefault(order.symbol, Position())
        if order.side is OrderSide.BUY:
            total_cost = fill_price * order.quantity
            if total_cost > self.cash:
                raise ValueError("paper account has insufficient cash")
            self.cash -= total_cost
            total_quantity = position.quantity + order.quantity
            position.average_price = (
                (position.average_price * position.quantity) + total_cost
            ) / total_quantity
            position.quantity = total_quantity
        else:
            if order.quantity > position.quantity:
                raise ValueError("paper account cannot sell more than the position")
            self.cash += fill_price * order.quantity
            self.realized_pnl += (fill_price - position.average_price) * order.quantity
            position.quantity -= order.quantity
            if position.quantity == 0:
                position.average_price = Decimal("0")
        order.status = OrderStatus.FILLED
        order.fill_price = fill_price

    def _cancel_other_open_exits(self, filled_order: PaperOrder) -> None:
        for order in self.orders.values():
            if (
                order.order_id != filled_order.order_id
                and order.symbol == filled_order.symbol
                and order.side is OrderSide.SELL
                and order.status is OrderStatus.OPEN
            ):
                order.status = OrderStatus.CANCELED

    @staticmethod
    def _decimal_or_none(value: float | None) -> Decimal | None:
        return Decimal(str(value)) if value is not None else None