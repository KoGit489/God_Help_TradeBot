from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from .paper import OrderSide, OrderStatus, OrderType, PaperOrder, Position, Quote


@dataclass(frozen=True)
class OrderPreview:
    symbol: str
    side: OrderSide
    quantity: int
    order_type: OrderType
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    dry_run: bool = True
    sandbox_mode: bool = True
    estimated_fill_price: Decimal | None = None


def _load_dotenv_file(env_file: str | Path | None = None) -> dict[str, str]:
    path = Path(env_file) if env_file else Path.cwd() / ".env"
    values: dict[str, str] = {}
    if not path.exists():
        return values
    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


@dataclass(frozen=True)
class WebullSessionConfig:
    api_key: str | None = None
    api_secret: str | None = None
    account_id: str | None = None
    sandbox_mode: bool = True
    live_mode: bool = False

    def __post_init__(self) -> None:
        if self.live_mode and self.sandbox_mode:
            raise ValueError("live_mode and sandbox_mode cannot both be true")
        if self.live_mode and not (self.api_key and self.api_secret and self.account_id):
            raise ValueError("live_mode requires api_key, api_secret, and account_id")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WebullSessionConfig":
        data = os.environ if env is None else dict(env)
        dotenv_values = _load_dotenv_file()
        merged = {**dotenv_values, **data}
        return cls(
            api_key=merged.get("WEBULL_API_KEY"),
            api_secret=merged.get("WEBULL_API_SECRET"),
            account_id=merged.get("WEBULL_ACCOUNT_ID"),
            sandbox_mode=True,
            live_mode=False,
        )


WEBULL_REGION = "us"
WEBULL_SANDBOX_ENDPOINT = "api.sandbox.webull.com"
US_STOCK_CATEGORY = "US_STOCK"
US_MARKET = "US"
EQUITY_INSTRUMENT = "EQUITY"
NORMAL_COMBO = "NORMAL"
QTY_ENTRUST = "QTY"
VALID_TRADING_SESSIONS = {"CORE", "ALL", "NIGHT"}

WEBULL_ORDER_TYPES = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.STOP: "STOP_LOSS",
}

WEBULL_STATUS_MAP = {
    "FILLED": OrderStatus.FILLED,
    "CANCELLED": OrderStatus.CANCELED,
    "CANCELED": OrderStatus.CANCELED,
}


@dataclass
class WebullSdkSession:
    """Authenticated Webull SDK clients bound to the sandbox endpoint."""

    trade_client: Any
    data_client: Any
    account_id: str | None = None


@runtime_checkable
class BrokerAdapter(Protocol):
    """Common broker contract that keeps strategy code independent of the execution backend."""

    def submit_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType,
        *,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> PaperOrder:
        ...

    def get_positions(self) -> dict[str, Position]:
        ...

    def get_position(self, symbol: str) -> Position | None:
        ...

    def get_latest_quote(self, symbol: str) -> Quote | None:
        ...

    def cancel_order(self, order_id: int) -> PaperOrder | None:
        ...


class WebullSandboxBroker:
    """Read-only Webull sandbox adapter backed by the official Webull OpenAPI SDK.

    The adapter fetches quotes and positions from the sandbox environment. Order submission
    stays intentionally disabled and `live_mode` cannot be enabled from this adapter.
    """

    def __init__(
        self,
        session: WebullSdkSession | None = None,
        *,
        config: WebullSessionConfig | None = None,
        sandbox_mode: bool = True,
        live_mode: bool = False,
    ) -> None:
        resolved_config = config or WebullSessionConfig.from_env()
        if live_mode:
            resolved_config = WebullSessionConfig(
                api_key=resolved_config.api_key,
                api_secret=resolved_config.api_secret,
                account_id=resolved_config.account_id,
                sandbox_mode=False,
                live_mode=True,
            )
        else:
            resolved_config = WebullSessionConfig(
                api_key=resolved_config.api_key,
                api_secret=resolved_config.api_secret,
                account_id=resolved_config.account_id,
                sandbox_mode=sandbox_mode,
                live_mode=False,
            )

        self.session = session
        self.config = resolved_config
        self.sandbox_mode = resolved_config.sandbox_mode
        self.live_mode = resolved_config.live_mode
        self._quote_cache: dict[str, Quote] = {}
        self._resolved_account_id: str | None = None
        self._orders: dict[int, PaperOrder] = {}
        self._next_order_id = 1

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "WebullSandboxBroker":
        return cls(config=WebullSessionConfig.from_env(env))

    def connect(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        account_id: str | None = None,
        sandbox_mode: bool = True,
        live_mode: bool = False,
    ) -> "WebullSandboxBroker":
        self.config = WebullSessionConfig(
            api_key=api_key or self.config.api_key,
            api_secret=api_secret or self.config.api_secret,
            account_id=account_id or self.config.account_id,
            sandbox_mode=sandbox_mode,
            live_mode=live_mode,
        )
        self.sandbox_mode = self.config.sandbox_mode
        self.live_mode = self.config.live_mode
        self.session = self._build_sdk_session()
        self._resolved_account_id = None
        self._orders = {}
        self._next_order_id = 1
        return self

    def _build_sdk_session(self) -> WebullSdkSession:
        if self.live_mode:
            raise RuntimeError("live_mode is intentionally disabled until sandbox validation is complete")
        if not self.config.api_key or not self.config.api_secret:
            raise ValueError("connect() requires api_key and api_secret")

        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient
            from webull.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError(
                "Install the official SDK first: pip install webull-openapi-python-sdk"
            ) from exc

        api_client = ApiClient(self.config.api_key, self.config.api_secret, WEBULL_REGION)
        api_client.add_endpoint(WEBULL_REGION, WEBULL_SANDBOX_ENDPOINT)
        return WebullSdkSession(
            trade_client=TradeClient(api_client),
            data_client=DataClient(api_client),
            account_id=self.config.account_id,
        )

    def _resolve_account_id(self) -> str:
        self._require_session()
        if self._resolved_account_id:
            return self._resolved_account_id

        response = self.session.trade_client.account_v2.get_account_list()
        accounts = _payload_records(response)
        configured = self.session.account_id
        chosen = next(
            (a for a in accounts if configured and str(a.get("account_id")) == str(configured)),
            None,
        )
        if chosen is None and accounts:
            chosen = accounts[0]
        account_id = str(chosen.get("account_id")) if chosen else ""
        if not account_id:
            raise RuntimeError("No sandbox accounts are available for this API key")
        self._resolved_account_id = account_id
        return account_id

    def _validate_order_params(
        self,
        symbol: str,
        quantity: int,
        order_type: OrderType,
        limit_price: float | None,
        stop_price: float | None,
    ) -> str:
        if not symbol or not symbol.strip():
            raise ValueError("symbol is required")
        if quantity < 1:
            raise ValueError("quantity must be positive")
        if order_type is OrderType.LIMIT and limit_price is None:
            raise ValueError("limit_price is required for limit orders")
        if order_type is OrderType.STOP and stop_price is None:
            raise ValueError("stop_price is required for stop orders")
        return symbol.strip().upper()

    def _build_order_payload(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType,
        *,
        limit_price: float | None = None,
        stop_price: float | None = None,
        client_order_id: str | None = None,
        trading_session: str = "CORE",
    ) -> dict[str, Any]:
        ticker = self._validate_order_params(symbol, quantity, order_type, limit_price, stop_price)
        session = trading_session.upper()
        if session not in VALID_TRADING_SESSIONS:
            raise ValueError(f"trading_session must be one of {sorted(VALID_TRADING_SESSIONS)}")
        payload: dict[str, Any] = {
            "client_order_id": client_order_id or uuid.uuid4().hex,
            "combo_type": NORMAL_COMBO,
            "symbol": ticker,
            "instrument_type": EQUITY_INSTRUMENT,
            "market": US_MARKET,
            "order_type": WEBULL_ORDER_TYPES[order_type],
            "quantity": str(quantity),
            "side": side.value,
            "time_in_force": "DAY",
            "support_trading_session": session,
            "entrust_type": QTY_ENTRUST,
        }
        if order_type is OrderType.LIMIT:
            payload["limit_price"] = str(Decimal(str(limit_price)))
        if order_type is OrderType.STOP:
            payload["stop_price"] = str(Decimal(str(stop_price)))
        return payload

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
        self._require_session()
        if self.live_mode:
            raise RuntimeError("live order submission is intentionally disabled")

        payload = self._build_order_payload(
            symbol,
            side,
            quantity,
            order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            trading_session=trading_session,
        )
        response = self.session.trade_client.order_v3.place_order(
            self._resolve_account_id(), [payload]
        )
        status = getattr(response, "status_code", None)
        if status != 200:
            raise RuntimeError(f"Webull sandbox order placement failed with HTTP {status}")

        order = PaperOrder(
            order_id=self._next_order_id,
            symbol=payload["symbol"],
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=Decimal(str(limit_price)) if limit_price is not None else None,
            stop_price=Decimal(str(stop_price)) if stop_price is not None else None,
            client_order_id=payload["client_order_id"],
        )
        self._orders[order.order_id] = order
        self._next_order_id += 1
        return order

    def preview_order_remote(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType,
        *,
        limit_price: float | None = None,
        stop_price: float | None = None,
        trading_session: str = "CORE",
    ) -> dict[str, Any]:
        """Ask the Webull sandbox for a real cost/fee estimate without placing the order."""
        self._require_session()
        if self.live_mode:
            raise RuntimeError("live order preview is intentionally disabled")

        payload = self._build_order_payload(
            symbol,
            side,
            quantity,
            order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            trading_session=trading_session,
        )
        response = self.session.trade_client.order_v3.preview_order(
            self._resolve_account_id(), [payload]
        )
        status = getattr(response, "status_code", None)
        if status != 200:
            raise RuntimeError(f"Webull sandbox order preview failed with HTTP {status}")
        data = response.json()
        return data if isinstance(data, dict) else {"data": data}

    def preview_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: OrderType,
        *,
        limit_price: float | None = None,
        stop_price: float | None = None,
    ) -> OrderPreview:
        ticker = self._validate_order_params(symbol, quantity, order_type, limit_price, stop_price)

        estimated = Decimal(str(limit_price if limit_price is not None else stop_price if stop_price is not None else 0.0))
        return OrderPreview(
            symbol=ticker,
            side=side,
            quantity=quantity,
            order_type=order_type,
            limit_price=Decimal(str(limit_price)) if limit_price is not None else None,
            stop_price=Decimal(str(stop_price)) if stop_price is not None else None,
            dry_run=True,
            sandbox_mode=self.sandbox_mode,
            estimated_fill_price=estimated if estimated != Decimal("0") else None,
        )

    def get_positions(self) -> dict[str, Position]:
        self._require_session()
        response = self.session.trade_client.account_v2.get_account_position(self._resolve_account_id())
        positions: dict[str, Position] = {}
        for record in _payload_records(response):
            symbol = str(record.get("symbol") or record.get("ticker") or "").upper()
            if not symbol:
                continue
            positions[symbol] = Position(
                quantity=int(_decimal_field(record, "quantity", "position", "qty", default=Decimal("0"))),
                average_price=_decimal_field(
                    record, "average_price", "avg_price", "cost_price", "avg_cost", default=Decimal("0")
                ),
            )
        return positions

    def get_position(self, symbol: str) -> Position | None:
        return self.get_positions().get(symbol.upper())

    def get_latest_quote(self, symbol: str) -> Quote | None:
        ticker = symbol.upper()
        if self.session is None:
            return self._quote_cache.get(ticker)
        response = self.session.data_client.market_data.get_snapshot(ticker, US_STOCK_CATEGORY)
        records = _payload_records(response)
        if not records:
            return self._quote_cache.get(ticker)
        quote = _quote_from_snapshot(records[0])
        self._quote_cache[quote.symbol] = quote
        return quote

    def get_latest_quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Fetch snapshots for many symbols in one batched request (max 100 per call)."""
        tickers = list(dict.fromkeys(s.upper() for s in symbols if s and s.strip()))
        if not tickers or self.session is None:
            return {}
        response = self.session.data_client.market_data.get_snapshot(tickers, US_STOCK_CATEGORY)
        quotes: dict[str, Quote] = {}
        for record in _payload_records(response):
            try:
                quote = _quote_from_snapshot(record)
            except ValueError:
                continue
            quotes[quote.symbol] = quote
            self._quote_cache[quote.symbol] = quote
        return quotes

    def set_quote(self, quote: Quote) -> Quote:
        self._quote_cache[quote.symbol.upper()] = quote
        return quote

    def cancel_order(self, order_id: int | str) -> PaperOrder | None:
        """Cancel an open sandbox order by local order id or Webull client_order_id."""
        self._require_session()
        if self.live_mode:
            raise RuntimeError("live order cancellation is intentionally disabled")

        if isinstance(order_id, int):
            order = self._orders.get(order_id)
            if order is None or not order.client_order_id:
                return None
            client_order_id = order.client_order_id
        else:
            client_order_id = order_id
            order = next(
                (o for o in self._orders.values() if o.client_order_id == client_order_id),
                None,
            )

        response = self.session.trade_client.order_v3.cancel_order(
            self._resolve_account_id(), client_order_id
        )
        status = getattr(response, "status_code", None)
        if status != 200:
            raise RuntimeError(f"Webull sandbox order cancellation failed with HTTP {status}")
        if order is not None:
            order.status = OrderStatus.CANCELED
        return order

    def get_order_status(self, client_order_id: str) -> dict[str, Any] | None:
        """Fetch one sandbox order's live status record by client_order_id."""
        self._require_session()
        response = self.session.trade_client.order_v3.get_order_detail(
            self._resolve_account_id(), client_order_id
        )
        payload = response.json()
        orders = payload.get("orders", []) if isinstance(payload, dict) else []
        return orders[0] if orders else None

    def sync_orders(self) -> None:
        """Refresh locally tracked open orders with their real sandbox status and fills."""
        for order in list(self._orders.values()):
            if order.status is not OrderStatus.OPEN or not order.client_order_id:
                continue
            record = self.get_order_status(order.client_order_id)
            if record is None:
                continue
            mapped = WEBULL_STATUS_MAP.get(str(record.get("status", "")).upper())
            if mapped is OrderStatus.FILLED:
                order.status = OrderStatus.FILLED
                filled_avg = record.get("filled_avg_price") or record.get("avg_price")
                if filled_avg not in (None, ""):
                    order.fill_price = Decimal(str(filled_avg))
            elif mapped is OrderStatus.CANCELED:
                order.status = OrderStatus.CANCELED

    def open_orders(self) -> list[PaperOrder]:
        """Locally tracked orders still open after the latest sync."""
        return [o for o in self._orders.values() if o.status is OrderStatus.OPEN]

    def _require_session(self) -> None:
        if self.session is None:
            raise RuntimeError(
                "WebullSandboxBroker requires a configured sandbox session before any order or position calls are made."
            )


def _payload_records(response: Any) -> list[dict[str, Any]]:
    status = getattr(response, "status_code", None)
    if status is not None and status != 200:
        raise RuntimeError(f"Webull API request failed with HTTP {status}")
    payload = response.json()
    if isinstance(payload, dict):
        payload = payload.get("data", [])
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def _decimal_field(
    record: dict[str, Any],
    *names: str,
    default: Decimal | None = None,
) -> Decimal:
    for name in names:
        value = record.get(name)
        if value not in (None, ""):
            return Decimal(str(value))
    if default is not None:
        return default
    raise ValueError(f"record is missing a numeric value for {names[0]}")


def _quote_from_snapshot(record: dict[str, Any]) -> Quote:
    last = _decimal_field(record, "price", "close", "last")
    symbol = str(record.get("symbol") or record.get("ticker") or "").upper()
    if not symbol:
        raise ValueError("snapshot record is missing a symbol")
    return Quote(
        symbol=symbol,
        bid=_decimal_field(record, "bid", default=last),
        ask=_decimal_field(record, "ask", default=last),
        last=last,
    )


__all__ = ["BrokerAdapter", "OrderPreview", "WebullSandboxBroker", "WebullSdkSession", "WebullSessionConfig"]
