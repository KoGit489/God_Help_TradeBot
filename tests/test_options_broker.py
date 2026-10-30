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
