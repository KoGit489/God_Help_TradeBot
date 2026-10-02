from decimal import Decimal

from god_help_tradebot import OrderSide, OrderStatus, OrderType, PaperBroker, Quote
from god_help_tradebot.broker import BrokerAdapter, OrderPreview, WebullSandboxBroker, WebullSdkSession
from god_help_tradebot.settings import load_webull_config


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


def test_paper_broker_matches_adapter_contract() -> None:
    broker = PaperBroker(1_000)
    assert isinstance(broker, BrokerAdapter)

    order = broker.submit_order("TEST", OrderSide.BUY, 10, OrderType.MARKET)
    broker.process_quote(quote(11.00, 11.05, 11.02))

    assert broker.get_position("TEST").quantity == 10
    assert broker.get_positions()["TEST"].quantity == 10
    assert broker.get_latest_quote("TEST").last == Decimal("11.02")

    open_order = broker.submit_order("TEST", OrderSide.SELL, 2, OrderType.LIMIT, limit_price=12.00)
    assert broker.cancel_order(open_order.order_id) is open_order
    assert open_order.status is OrderStatus.CANCELED


def test_webull_sandbox_adapter_is_safe_by_default() -> None:
    adapter = WebullSandboxBroker()
    assert adapter.sandbox_mode is True
    assert adapter.live_mode is False

    try:
        adapter.get_positions()
        raise AssertionError("WebullSandboxBroker should not fetch positions without a configured session")
    except RuntimeError:
        pass


def test_webull_preview_order_is_dry_run_only() -> None:
    adapter = WebullSandboxBroker()
    preview = adapter.preview_order("TEST", OrderSide.BUY, 5, OrderType.LIMIT, limit_price=12.25)

    assert isinstance(preview, OrderPreview)
    assert preview.dry_run is True
    assert preview.symbol == "TEST"
    assert preview.side is OrderSide.BUY
    assert preview.quantity == 5
    assert preview.limit_price == Decimal("12.25")


def test_load_webull_config_reads_dotenv_file(monkeypatch, tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("WEBULL_API_KEY=demo_key\nWEBULL_API_SECRET=demo_secret\nWEBULL_ACCOUNT_ID=abc123\n", encoding="utf-8")

    monkeypatch.delenv("WEBULL_API_KEY", raising=False)
    monkeypatch.delenv("WEBULL_API_SECRET", raising=False)
    monkeypatch.delenv("WEBULL_ACCOUNT_ID", raising=False)

    config = load_webull_config(env_file=env_path)

    assert config.api_key == "demo_key"
    assert config.api_secret == "demo_secret"
    assert config.account_id == "abc123"
    assert config.sandbox_mode is True
    assert config.live_mode is False


def test_webull_session_config_from_env_reads_dotenv_file(monkeypatch, tmp_path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("WEBULL_API_KEY=demo_key\nWEBULL_API_SECRET=demo_secret\nWEBULL_ACCOUNT_ID=abc123\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEBULL_API_KEY", raising=False)
    monkeypatch.delenv("WEBULL_API_SECRET", raising=False)
    monkeypatch.delenv("WEBULL_ACCOUNT_ID", raising=False)

    config = WebullSandboxBroker.from_env().config

    assert config.api_key == "demo_key"
    assert config.api_secret == "demo_secret"
    assert config.account_id == "abc123"
    assert config.sandbox_mode is True
    assert config.live_mode is False


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _FakeMarketData:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self._status_code = status_code

    def get_snapshot(self, symbols, category):
        return _FakeResponse(self._payload, self._status_code)


class _FakeDataClient:
    def __init__(self, payload, status_code: int = 200) -> None:
        self.market_data = _FakeMarketData(payload, status_code)


class _FakeAccountV2:
    def __init__(self, accounts=None, positions=None) -> None:
        self._accounts = accounts or []
        self._positions = positions or []
        self.requested_account_id = None

    def get_account_list(self):
        return _FakeResponse(self._accounts)

    def get_account_position(self, account_id):
        self.requested_account_id = account_id
        return _FakeResponse(self._positions)


class _FakeTradeClient:
    def __init__(self, account_v2) -> None:
        self.account_v2 = account_v2


def _sdk_session(accounts=None, positions=None, quote_payload=None, account_id="cfg-1", quote_status: int = 200) -> WebullSdkSession:
    return WebullSdkSession(
        trade_client=_FakeTradeClient(_FakeAccountV2(accounts, positions)),
        data_client=_FakeDataClient(quote_payload or [], quote_status),
        account_id=account_id,
    )


def test_webull_quote_maps_snapshot_to_quote() -> None:
    session = _sdk_session(quote_payload=[{"symbol": "NIVF", "price": "0.1010", "bid": "0.10", "ask": "0.11"}])
    adapter = WebullSandboxBroker(session=session)

    result = adapter.get_latest_quote("nivf")

    assert result.symbol == "NIVF"
    assert result.last == Decimal("0.1010")
    assert result.bid == Decimal("0.10")
    assert result.ask == Decimal("0.11")
    # With a live session the adapter refetches every call; the cache is only an offline fallback.
    assert adapter._quote_cache["NIVF"] == result


def test_webull_quote_http_error_raises() -> None:
    session = _sdk_session(quote_payload={"error": "nope"}, quote_status=500)
    adapter = WebullSandboxBroker(session=session)

    try:
        adapter.get_latest_quote("NIVF")
        raise AssertionError("expected RuntimeError on non-200 response")
    except RuntimeError:
        pass


def test_webull_positions_map_from_sandbox() -> None:
    account_v2 = _FakeAccountV2(
        accounts=[{"account_id": "a1"}, {"account_id": "cfg-1"}],
        positions=[{"symbol": "NIVF", "quantity": "10", "cost_price": "0.25"}],
    )
    session = WebullSdkSession(
        trade_client=_FakeTradeClient(account_v2),
        data_client=_FakeDataClient([]),
        account_id="cfg-1",
    )
    adapter = WebullSandboxBroker(session=session)

    positions = adapter.get_positions()

    assert account_v2.requested_account_id == "cfg-1"
    assert positions["NIVF"].quantity == 10
    assert positions["NIVF"].average_price == Decimal("0.25")
    assert adapter.get_position("OTHER") is None


def test_webull_positions_fall_back_to_first_sandbox_account() -> None:
    account_v2 = _FakeAccountV2(accounts=[{"account_id": "sandbox-first"}], positions=[])
    session = WebullSdkSession(
        trade_client=_FakeTradeClient(account_v2),
        data_client=_FakeDataClient([]),
        account_id="stale-config-id",
    )
    adapter = WebullSandboxBroker(session=session)

    assert adapter.get_positions() == {}
    assert account_v2.requested_account_id == "sandbox-first"


def test_webull_submit_order_stays_disabled_with_session() -> None:
    adapter = WebullSandboxBroker(session=_sdk_session())

    try:
        adapter.submit_order("NIVF", OrderSide.BUY, 1, OrderType.MARKET)
        raise AssertionError("order submission must stay disabled")
    except NotImplementedError:
        pass


def test_webull_connect_requires_credentials(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WEBULL_API_KEY", raising=False)
    monkeypatch.delenv("WEBULL_API_SECRET", raising=False)
    monkeypatch.delenv("WEBULL_ACCOUNT_ID", raising=False)

    adapter = WebullSandboxBroker()
    try:
        adapter.connect()
        raise AssertionError("connect() should require credentials")
    except ValueError:
        pass