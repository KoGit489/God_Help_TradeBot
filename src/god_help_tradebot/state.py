"""Persistent storage for monitoring state.

Exit plans are saved to a JSON file so a bot restart never leaves an open
position unmanaged. The store is a thin, atomic-write JSON file — no database
required for a single-machine bot.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .config import BotConfig
from .monitor import ExitPlan
from .paper import PaperOrder, Position
from .schedule import EASTERN, get_trading_session

DEFAULT_STATE_FILE = Path("bot_state.json")


class StateStore:
    """Load and save ExitPlans to a JSON file with atomic writes."""

    def __init__(self, path: str | Path = DEFAULT_STATE_FILE) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, ExitPlan]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        plans = data.get("exit_plans", {})
        return {
            symbol: ExitPlan(
                symbol=symbol,
                quantity=int(record["quantity"]),
                target_price=Decimal(str(record["target_price"])),
                stop_price=Decimal(str(record["stop_price"])),
                direction=record.get(
                    "direction",
                    "short" if Decimal(str(record["target_price"])) < Decimal(str(record["stop_price"])) else "long",
                ),
                liquidation_requested=bool(record.get("liquidation_requested", False)),
            )
            for symbol, record in plans.items()
            if isinstance(record, dict)
        }

    def save(self, plans: dict[str, ExitPlan]) -> None:
        payload = {
            "exit_plans": {
                symbol: {
                    "quantity": plan.quantity,
                    "target_price": str(plan.target_price),
                    "stop_price": str(plan.stop_price),
                    "direction": plan.direction,
                    "liquidation_requested": plan.liquidation_requested,
                }
                for symbol, plan in plans.items()
            }
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def clear(self, symbol: str, plans: dict[str, ExitPlan]) -> None:
        """Drop one symbol's plan and persist the rest."""
        plans.pop(symbol, None)
        self.save(plans)


class EquityPerformanceStore:
    def __init__(self, path: str | Path, account_id: str) -> None:
        self.path = Path(path)
        self.data = {
            "version": 1, "account_id": account_id, "trades": [], "reviews": [],
            "active": {}, "orders": {}, "fills": {}, "adaptive": {},
            "reviewed_count": 0, "paused": False, "take_profit_sessions": {},
        }
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("account_id") != account_id:
                    raise ValueError("account mismatch")
                for key in ("trades", "reviews"):
                    if not isinstance(loaded.get(key), list) or any(not isinstance(item, dict) for item in loaded[key]):
                        raise ValueError("invalid ledger")
                for key in ("active", "orders", "fills", "adaptive"):
                    if not isinstance(loaded.get(key), dict):
                        raise ValueError("invalid ledger")
                self.data.update(loaded)
                sessions = self.data["take_profit_sessions"]
                if not isinstance(sessions, dict) or any(
                    datetime.fromisoformat(day).date().isoformat() != day
                    or (result is not None and not isinstance(result, bool))
                    for day, result in sessions.items()
                ):
                    raise ValueError("invalid take-profit sessions")
                if loaded.get("version") != 1 or not isinstance(self.data["paused"], bool):
                    raise ValueError("invalid ledger version or pause flag")
                if any(trade.get("outcome") not in ("win", "loss", "flat") for trade in loaded["trades"]):
                    raise ValueError("invalid trade outcome")
                allowed = {"min_relative_volume", "min_signals_present", "max_spread_percent", "min_confirmation_score"}
                if not set(loaded["adaptive"]) <= allowed or any(
                    not isinstance(value, (int, float)) or not Decimal(str(value)).is_finite() or value <= 0
                    for value in loaded["adaptive"].values()
                ):
                    raise ValueError("invalid adaptive gates")
                for order in loaded["orders"].values():
                    if not isinstance(order, dict) or order["role"] not in ("ENTRY", "EXIT") or order["side"] not in ("BUY", "SELL", "SHORT") or not isinstance(order["quantity"], int) or order["quantity"] <= 0 or not order["symbol"]:
                        raise ValueError("invalid tracked order")
                for trade in list(loaded["active"].values()) + loaded["trades"]:
                    if not isinstance(trade, dict) or trade["direction"] not in ("long", "short"):
                        raise ValueError("invalid trade")
                    if not isinstance(trade["entry_quantity"], int) or not isinstance(trade["exit_quantity"], int) or not 0 <= trade["exit_quantity"] <= trade["entry_quantity"] or trade["entry_quantity"] <= 0:
                        raise ValueError("invalid trade quantities")
                    if any(not Decimal(str(trade[field])).is_finite() or Decimal(str(trade[field])) < 0 for field in ("entry_notional", "exit_notional")):
                        raise ValueError("invalid execution amounts")
                for fill in loaded["fills"].values():
                    if not isinstance(fill, dict) or not isinstance(fill["quantity"], int) or fill["quantity"] < 0 or not Decimal(str(fill["notional"])).is_finite() or Decimal(str(fill["notional"])) < 0:
                        raise ValueError("invalid fill checkpoint")
                if not isinstance(self.data["reviewed_count"], int) or not 0 <= self.data["reviewed_count"] <= len(self.data["trades"]):
                    raise ValueError("invalid review count")
            except (OSError, ValueError, TypeError, KeyError, InvalidOperation) as error:
                raise RuntimeError("Equity performance ledger unreadable or belongs to another account; reconcile before startup") from error
        else:
            self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self.data, indent=2) + "\n", encoding="utf-8")
        temp.replace(self.path)

    def key(self, order: PaperOrder) -> str:
        return order.client_order_id or str(order.order_id)

    @property
    def recovery_ids(self) -> frozenset[str]:
        return frozenset(self.data["orders"])

    def track_order(self, order: PaperOrder, role: str) -> None:
        key = self.key(order)
        if key not in self.data["orders"]:
            self.data["orders"][key] = {
                "symbol": order.symbol, "side": order.side.value,
                "quantity": order.quantity, "role": role,
            }
            self.save()

    def record_fill(self, order: PaperOrder, role: str, now: datetime) -> None:
        key = self.key(order)
        quantity = getattr(order, "filled_quantity", 0)
        if not quantity and order.status.value == "FILLED":
            quantity = order.quantity
        if not quantity or order.fill_price is None:
            if quantity and order.fill_price is None:
                self.data["paused"] = True
                self.data["pause_reason"] = "Filled quantity has no execution price; reconcile performance ledger"
                self.save()
            if order.status.value == "CANCELED" and not quantity:
                if self.data["orders"].pop(key, None) is not None:
                    self.save()
            return
        price = Decimal(str(order.fill_price))
        if not price.is_finite() or price <= 0:
            raise RuntimeError("Unpriced equity fill; performance reconciliation required")
        prior = self.data["fills"].get(key, {"quantity": 0, "notional": "0"})
        delta = quantity - prior["quantity"]
        if delta < 0:
            raise RuntimeError("Equity filled quantity moved backwards; reconciliation required")
        if delta == 0:
            if price * quantity != Decimal(prior["notional"]):
                self.data["paused"] = True
                self.data["pause_reason"] = "Execution price changed after recording; manual P&L reconciliation required"
                self.save()
                return
            if order.status.value != "OPEN":
                if self.data["orders"].pop(key, None) is not None:
                    self.save()
            return
        notional = price * quantity
        incremental = notional - Decimal(prior["notional"])
        trade = self.data["active"].get(order.symbol)
        if role == "ENTRY":
            if trade is None:
                trade = {
                    "trade_id": uuid.uuid4().hex, "symbol": order.symbol,
                    "direction": "short" if order.side.value == "SHORT" else "long",
                    "opened_at": now.isoformat(), "entry_quantity": 0,
                    "entry_notional": "0", "exit_quantity": 0, "exit_notional": "0",
                }
                self.data["active"][order.symbol] = trade
            trade["entry_quantity"] += delta
            trade["entry_notional"] = str(Decimal(trade["entry_notional"]) + incremental)
        elif trade is None:
            self.data["paused"] = True
            self.data["pause_reason"] = "Exit fill lacks a recorded entry; reconcile performance ledger"
        else:
            if trade["exit_quantity"] + delta > trade["entry_quantity"]:
                self.data["paused"] = True
                self.data["pause_reason"] = "Equity exits exceed recorded entries; reconciliation required"
                self.save()
                return
            trade["exit_quantity"] += delta
            trade["exit_notional"] = str(Decimal(trade["exit_notional"]) + incremental)
        self.data["fills"][key] = {"quantity": quantity, "notional": str(notional)}
        if order.status.value != "OPEN":
            self.data["orders"].pop(key, None)
        self.save()

    def finalize(self, positions: dict[str, Position], now: datetime, config: BotConfig,
                 min_confirmation: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        completed = []
        for symbol, trade in list(self.data["active"].items()):
            position = positions.get(symbol)
            if position is not None and position.quantity:
                continue
            if any(order["symbol"] == symbol for order in self.data["orders"].values()):
                continue
            if trade["entry_quantity"] != trade["exit_quantity"]:
                if not trade["exit_quantity"]:
                    continue
                self.data["paused"] = True
                self.data["pause_reason"] = "Position disappeared without matching exit fills; manual reconciliation required"
                continue
            pnl = Decimal(trade["exit_notional"]) - Decimal(trade["entry_notional"])
            if trade["direction"] == "short":
                pnl = -pnl
            record = {**trade, "closed_at": now.isoformat(), "realized_pnl_usd": str(pnl.quantize(Decimal("0.01"))),
                      "entry_average_price": str(Decimal(trade["entry_notional"]) / trade["entry_quantity"]),
                      "exit_average_price": str(Decimal(trade["exit_notional"]) / trade["exit_quantity"]),
                      "outcome": "win" if pnl > 0 else "loss" if pnl < 0 else "flat"}
            self.data["trades"].append(record)
            completed.append(record)
            del self.data["active"][symbol]
        reviews = []
        while len(self.data["trades"]) - self.data["reviewed_count"] >= 5:
            start = self.data["reviewed_count"]
            window = self.data["trades"][start:start + 5]
            wins = sum(trade["outcome"] == "win" for trade in window)
            changes = {}
            if wins < 4:
                rules = {
                    "min_relative_volume": (0.25, 5.0), "min_signals_present": (1, 4),
                    "max_spread_percent": (-0.1, 0.3), "min_confirmation_score": (0.03, 0.90),
                }
                for name, (step, limit) in rules.items():
                    current = self.data["adaptive"].get(name, min_confirmation if name == "min_confirmation_score" else getattr(config, name))
                    updated = min(current + step, limit) if step > 0 else max(current + step, limit)
                    if (step > 0 and updated > current) or (step < 0 and updated < current):
                        changes[name] = round(updated, 4)
                self.data["adaptive"].update(changes)
                if not changes:
                    self.data["paused"] = True
                    self.data["pause_reason"] = "Four-of-five trade target missed at all tightening limits"
            review = {"through_trade": start + 5, "reviewed_at": now.isoformat(),
                      "wins": wins, "trades": 5, "target_wins": 4, "changes": changes,
                      "paused": self.data["paused"]}
            self.data["reviews"].append(review)
            self.data["reviewed_count"] += 5
            reviews.append(review)
        if completed or reviews or self.data["paused"]:
            self.save()
        return completed, reviews

    def effective_config(self, config: BotConfig) -> BotConfig:
        overrides = {name: value for name, value in self.data["adaptive"].items() if name != "min_confirmation_score"}
        return replace(config, **overrides)

    @property
    def prioritize_prepop(self) -> bool:
        sessions = [result for _, result in sorted(self.data["take_profit_sessions"].items()) if result is not None]
        return any(
            sum(result is True for result in sessions[start:start + 5]) < 4
            for start in range(0, len(sessions) - 4, 5)
        )

    def review_take_profit(self, now: datetime, *, observe: bool) -> list[tuple[str, bool, int, int]]:
        now = now.astimezone(EASTERN)
        sessions = self.data["take_profit_sessions"]
        today = get_trading_session(now.date())
        if observe and today and today.market_open <= now < today.market_close and now.date().isoformat() not in sessions:
            sessions[now.date().isoformat()] = None
            self.save()
        if self.data["active"] or self.data["orders"]:
            return []
        candidates = {day: False for day, result in sessions.items() if result is None}
        for trade in self.data["trades"]:
            if not trade.get("closed_at"):
                continue
            day = datetime.fromisoformat(trade["closed_at"]).astimezone(EASTERN).date().isoformat()
            if sessions.get(day) is True:
                continue
            entry = Decimal(trade["entry_notional"])
            exit_value = Decimal(trade["exit_notional"])
            profit = entry - exit_value if trade["direction"] == "short" else exit_value - entry
            hit = entry > 0 and profit >= entry * Decimal("0.30")
            if sessions.get(day) is False:
                if hit:
                    candidates[day] = True
                continue
            candidates[day] = candidates.get(day, False) or hit
        results = []
        for day, hit in sorted(candidates.items()):
            session = get_trading_session(datetime.fromisoformat(day).date())
            if session is None or now < session.market_close:
                continue
            sessions[day] = hit
            closed = [result for date, result in sorted(sessions.items()) if date <= day and result is not None]
            window = closed[((len(closed) - 1) // 5) * 5:]
            results.append((day, hit, sum(window), len(window)))
        if results:
            self.save()
        return results


__all__ = ["StateStore", "EquityPerformanceStore", "DEFAULT_STATE_FILE"]
