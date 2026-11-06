from decimal import Decimal
from types import SimpleNamespace

import pytest

from god_help_tradebot.broker import WebullSandboxBroker
from god_help_tradebot.paper import OrderSide, OrderType


class _Response:
    status_code = 200


class _OrderV3:
    def __init__(self):
        self.calls = []

    def place_order(self, account_id, orders):
        self.calls.append((account_id, orders))
        return _Response()


def _broker(live_mode=False):
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    broker.live_mode = live_mode
    broker.sandbox_mode = not live_mode
    broker.session = SimpleNamespace(trade_client=SimpleNamespace(order_v3=_OrderV3()))
    broker._resolve_account_id = lambda: "paper-account"
    broker._orders = {}
    broker._next_order_id = 1
    return broker


def _account_broker(configured_id, accounts):
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    response = SimpleNamespace(status_code=200, json=lambda: accounts)
    broker.session = SimpleNamespace(
        account_id=configured_id,
        trade_client=SimpleNamespace(account_v2=SimpleNamespace(
            get_account_list=lambda: response
        )),
    )
    broker._resolved_account_id = None
    return broker


def test_account_resolution_uses_configured_account_not_first_account():
    broker = _account_broker("options-account", [
        {"account_id": "other-account"},
        {"account_id": "options-account"},
    ])

    assert broker._resolve_account_id() == "options-account"


def test_account_resolution_rejects_configured_account_not_in_api_list():
    broker = _account_broker("missing-account", [{"account_id": "other-account"}])

    with pytest.raises(RuntimeError, match="Configured Webull sandbox account ID"):
        broker._resolve_account_id()


def test_account_resolution_rejects_ambiguous_accounts_without_configured_id():
    broker = _account_broker(None, [
        {"account_id": "first-account"},
        {"account_id": "second-account"},
    ])

    with pytest.raises(RuntimeError, match="Multiple Webull sandbox accounts"):
        broker._resolve_account_id()


def test_account_resolution_uses_only_available_account_when_unconfigured():
    broker = _account_broker(None, [{"account_id": "only-account"}])

    assert broker._resolve_account_id() == "only-account"


def test_spy_option_market_order_uses_single_leg_buy_to_open_payload():
    broker = _broker()
    order = broker.submit_option_market_order(
        "SPY", "SPY261028C00680000", Decimal("680"), "2026-10-28", "CALL", 3,
        position_intent="BUY_TO_OPEN", client_order_id="client-1",
    )

    account_id, orders = broker.session.trade_client.order_v3.calls[0]
    payload = orders[0]
    assert account_id == "paper-account"
    assert payload["order_type"] == "MARKET"
    assert payload["instrument_type"] == "OPTION"
    assert payload["option_strategy"] == "SINGLE"
    assert payload["position_intent"] == "BUY_TO_OPEN"
    assert payload["quantity"] == "3"
    assert payload["legs"][0]["option_type"] == "CALL"
    assert payload["legs"][0]["option_expire_date"] == "2026-10-28"
    assert order.client_order_id == "client-1"


def test_spy_option_order_refuses_non_sandbox_mode():
    broker = _broker(live_mode=True)
    with pytest.raises(RuntimeError, match="sandbox-only"):
        broker.submit_option_market_order(
            "SPY", "SPY261028P00680000", Decimal("680"), "2026-10-28", "PUT", 1,
            position_intent="BUY_TO_OPEN",
        )


def test_spy_option_order_only_allows_long_open_and_close():
    broker = _broker()
    with pytest.raises(ValueError, match="only long option opens and closes"):
        broker.submit_option_market_order(
            "SPY", "SPY261028C00680000", Decimal("680"), "2026-10-28", "CALL", 1,
            position_intent="SELL_TO_OPEN",
        )


def test_remote_open_order_interlock_allows_only_persisted_recovery_order():
    response = SimpleNamespace(status_code=200, json=lambda: [{"client_order_id": "recover-me"}])
    broker = WebullSandboxBroker.__new__(WebullSandboxBroker)
    broker._resolve_account_id = lambda: "test-account"
    broker.session = SimpleNamespace(trade_client=SimpleNamespace(
        order_v3=SimpleNamespace(get_order_open=lambda *args, **kwargs: response)
    ))

    broker.assert_no_remote_open_orders(allowed_client_order_ids={"recover-me"})

    with pytest.raises(RuntimeError, match="startup blocked"):
        broker.assert_no_remote_open_orders(allowed_client_order_ids={"another-order"})


def test_buying_power_does_not_fall_back_to_net_liquidation_value():
    broker = _broker()
    payload = {"account_currency_assets": [{"buying_power": "0", "cash_balance": "50", "net_liquidation_value": "500"}]}
    account_ids = []

    def get_account_balance(account_id):
        account_ids.append(account_id)
        return SimpleNamespace(json=lambda: payload)

    broker.session.trade_client.account_v2 = SimpleNamespace(
        get_account_balance=get_account_balance
    )

    assert broker.get_buying_power() == Decimal("0")
    payload["account_currency_assets"][0]["buying_power"] = "25"
    assert broker.get_buying_power() == Decimal("25")
    assert account_ids == ["paper-account", "paper-account"]

    payload["account_currency_assets"][0].pop("buying_power")
    payload["account_currency_assets"][0].pop("cash_balance")
    assert broker.get_buying_power() is None


def test_option_chain_can_request_all_strikes_for_expiration():
    broker = _broker()
    calls = []
    broker.session.data_client = SimpleNamespace(instrument=SimpleNamespace(
        list_option_contracts=lambda **kwargs: calls.append(kwargs) or _Response()
    ))

    broker.get_option_contracts("SPY", "2026-10-28")

    assert calls == [{
        "category": "US_OPTION",
        "underlying_symbols": "SPY",
        "status": "LISTING",
        "start_date": "2026-10-28",
        "end_date": "2026-10-28",
        "strike_price_gte": None,
        "strike_price_lte": None,
        "show_deliverables": True,
        "pagination_key": None,
    }]
