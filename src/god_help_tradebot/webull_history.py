from __future__ import annotations

import math
import calendar
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .broker import US_STOCK_CATEGORY, is_rate_limit_error
from .orderflow import market_time
from .patterns import bars_from_response
from .research import read_json, write_json


class WebullHistoryCollector:
    def __init__(self, root: str | Path, get_bars: Callable, *, timespan: str = "M5") -> None:
        self.root = Path(root)
        self.get_bars = get_bars
        self.timespan = timespan
        if timespan not in ("M1", "M5", "M15", "M30", "M60", "M120", "M240", "D"):
            raise ValueError("unsupported historical research interval")
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "collection.json"

    def seed(self, symbols: list[str], now: datetime) -> None:
        state = read_json(self.state_path, {"version": 1, "symbols": {}, "offset": 0})
        for symbol in symbols:
            ticker = symbol.strip().upper()
            if not ticker or len(ticker) > 20 or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for character in ticker):
                raise ValueError("invalid historical research symbol")
            key = f"{ticker}-{self.timespan}"
            if key not in state["symbols"] and len(state["symbols"]) >= 200:
                continue
            floor = now.replace(year=now.year - 5, day=min(now.day, calendar.monthrange(now.year - 5, now.month)[1]))
            state["symbols"].setdefault(key, {"symbol": ticker, "timespan": self.timespan,
                                               "end_time": int((now - timedelta(minutes=20)).timestamp() * 1000),
                                               "start_time": int(floor.timestamp() * 1000),
                                               "status": "pending", "pages": 0, "bars": 0})
        write_json(self.state_path, state)

    def tick(self, now: datetime) -> dict[str, Any]:
        state = read_json(self.state_path, {"version": 1, "symbols": {}, "offset": 0})
        next_attempt = state.get("next_attempt")
        if next_attempt and now < datetime.fromisoformat(next_attempt):
            return {"status": "cooldown", "next_attempt": next_attempt}
        pending = [(key, item) for key, item in state["symbols"].items() if item["status"] in ("pending", "collecting", "rate_limited", "retry")]
        if not pending:
            return {"status": "idle", "symbols": len(state["symbols"])}
        index = state["offset"] % len(pending)
        key, item = pending[index]
        state["offset"] = index + 1
        state["next_attempt"] = (now + timedelta(seconds=60)).isoformat()
        end_time = item["end_time"]
        if sum(path.stat().st_size for path in (self.root / "bars").glob("**/*.json")) >= 512 * 1024 * 1024:
            item["status"] = "storage_budget_reached"
            write_json(self.state_path, state)
            self.report(state, now)
            return {"status": "storage_budget_reached", "symbol": item["symbol"]}
        try:
            options = {"end_time": end_time}
            if item["timespan"] != "D":
                options.update(trading_sessions=["PRE", "RTH", "ATH", "OVN"])
            response = self.get_bars([item["symbol"]], US_STOCK_CATEGORY, item["timespan"], count=1200,
                                     real_time_required=False, **options)
            status = response.status_code
            if status in (401, 403):
                item["status"] = "permission_blocked"
                item["reason"] = "Webull API permission/subscription required; no subscription purchased"
            elif status == 429:
                item["status"] = "rate_limited"
                state["next_attempt"] = (now + timedelta(minutes=5)).isoformat()
            elif status != 200:
                item["status"] = "retry"
                item["reason"] = f"HTTP {status}"
                state["next_attempt"] = (now + timedelta(minutes=5)).isoformat()
            else:
                self._accept_page(key, item, response.json(), end_time, now)
        except Exception as error:
            status = str(getattr(error, "http_status", getattr(error, "status_code", "")))
            if status in ("401", "403"):
                item["status"] = "permission_blocked"
                item["reason"] = "Webull API permission/subscription required; no subscription purchased"
            elif status == "417":
                item["status"] = "symbol_or_query_unavailable"
                item["reason"] = "Webull rejected the symbol or historical query; no guessed data used"
            elif is_rate_limit_error(error):
                item["status"] = "rate_limited"
            else:
                item["status"] = "retry"
                item["reason"] = type(error).__name__
            state["next_attempt"] = (now + timedelta(minutes=5)).isoformat()
        item["checked_at"] = now.isoformat()
        write_json(self.state_path, state)
        self.report(state, now)
        return {"symbol": item["symbol"], "timespan": item["timespan"], "status": item["status"],
                "oldest": item.get("oldest"), "bars": item["bars"]}

    def _accept_page(self, key: str, item: dict[str, Any], payload: Any, end_time: int, now: datetime) -> None:
        bars = bars_from_response(payload)
        if not bars:
            item["status"] = "exhausted"
            item["reason"] = "API returned no older bars; this is observed availability, not a promised retention limit"
            return
        unique = {}
        for bar in bars:
            timestamp = market_time(bar.time)
            if timestamp is None or any(not math.isfinite(value) or value <= 0 for value in (bar.open, bar.high, bar.low, bar.close)) or not math.isfinite(bar.volume) or bar.volume < 0:
                raise ValueError("invalid historical bar")
            if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
                raise ValueError("inconsistent historical OHLC")
            milliseconds = int(timestamp.timestamp() * 1000)
            if item["start_time"] <= milliseconds <= end_time and timestamp <= now:
                unique[milliseconds] = {"symbol": item["symbol"], "timespan": item["timespan"], "time": timestamp.isoformat(),
                                        "open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close, "volume": bar.volume}
        if not unique:
            reached_target = any((timestamp := market_time(bar.time)) is not None and int(timestamp.timestamp() * 1000) < item["start_time"] for bar in bars)
            item["status"] = "five_year_target_reached" if reached_target else "no_progress"
            item["reason"] = "No usable bars beyond local target or cursor; pagination stopped safely"
            return
        earliest = min(unique)
        cursor = earliest - 1
        if cursor >= end_time:
            item["status"] = "no_progress"
            return
        page = [unique[stamp] for stamp in sorted(unique)]
        future_low = item.get("future_low_close")
        drawdown = item.get("observed_close_drawdown_percent", 0.0)
        bar_range = item.get("max_bar_range_percent", 0.0)
        for bar in reversed(page):
            close = bar["close"]
            if future_low is not None:
                drawdown = max(drawdown, (close - future_low) / close * 100)
            future_low = min(future_low, close) if future_low is not None else close
            bar_range = max(bar_range, (bar["high"] - bar["low"]) / close * 100)
        directory = self.root / "bars" / key
        path = directory / f"{end_time}.json"
        existed = path.exists()
        write_json(path, {"source": "Webull OpenAPI", "retrieved_at": now.isoformat(), "end_time": end_time, "bars": page,
                          "adjustment": "forward-adjusted" if item["timespan"] == "D" else "unadjusted",
                          "has_historical_bid_ask": False, "has_historical_orderflow": False})
        if not existed:
            item["pages"] += 1
            item["bars"] += len(page)
        item["oldest"] = page[0]["time"]
        item["newest"] = item.get("newest", page[-1]["time"])
        item["end_time"] = cursor
        item["future_low_close"] = future_low
        item["observed_close_drawdown_percent"] = drawdown
        item["max_bar_range_percent"] = bar_range
        item["status"] = "five_year_target_reached" if cursor < item["start_time"] else "collecting"

    def report(self, state: dict[str, Any], now: datetime) -> None:
        write_json(self.root / "coverage.json", {
            "source": "Webull OpenAPI", "checked_at": now.isoformat(), "collection": state["symbols"],
            "historical_prices_only": True, "promotion_eligible": False,
            "missing": ["historical bid/ask and depth", "point-in-time confirmations and news", "historical orderflow",
                        "point-in-time delisted universe", "historical short borrow", "verified corporate actions"],
            "warning": "Collected bars are not fabricated executable quotes or full strategy evidence",
        })
        write_json(self.root / "price-context.json", {
            "diagnostic_only": True, "not_strategy_backtest": True, "checked_at": now.isoformat(),
            "series": [{key: item.get(key) for key in ("symbol", "timespan", "oldest", "newest", "bars", "status",
                        "observed_close_drawdown_percent", "max_bar_range_percent")} for item in state["symbols"].values()],
            "limitations": "Current selected-symbol universe; no executable historical bid/ask, fills, or full historical signals",
        })

    def retry_blocked(self) -> None:
        state = read_json(self.state_path, {"version": 1, "symbols": {}, "offset": 0})
        for item in state["symbols"].values():
            if item["status"] in ("permission_blocked", "retry", "no_progress", "symbol_or_query_unavailable"):
                item["status"] = "pending"
        state.pop("next_attempt", None)
        write_json(self.state_path, state)


class WebullHistoryResearch:
    def __init__(self, root: Path, get_bars: Callable, data_client: Any = None) -> None:
        self.minute = WebullHistoryCollector(root, get_bars, timespan="M5")
        self.daily = WebullHistoryCollector(root, get_bars, timespan="D")
        self.seeded_at: datetime | None = None
        self.input_collector = None
        if data_client is not None:
            from .historical_inputs import WebullInputCollector
            self.input_collector = WebullInputCollector(root / "inputs", data_client)

    def tick(self, loop: Any) -> dict[str, Any]:
        if loop.exit_plans or loop._pending_limit or loop._pending_exits or loop._rate_limit_backoff_active or loop._consecutive_errors:
            return {"status": "deferred_for_trading"}
        if loop.performance_store is not None and any(loop.performance_store.data[key] for key in ("paused", "active", "orders")):
            return {"status": "deferred_for_reconciliation"}
        now = loop._now()
        if self.seeded_at is None or now - self.seeded_at >= timedelta(minutes=5):
            snapshots = sorted(loop._discovery_cache, key=lambda snapshot: snapshot.symbol)
            symbols = [snapshot.symbol for snapshot in snapshots[:20]]
            if loop.performance_store is not None:
                symbols += [trade["symbol"] for trade in loop.performance_store.data["trades"][-20:]]
            symbols = list(dict.fromkeys(symbols))[:20]
            self.minute.seed(symbols, now)
            self.daily.seed(symbols, now)
            if self.input_collector is not None:
                self.input_collector.seed(symbols)
            self.seeded_at = now
        shared = read_json(self.minute.state_path, {})
        if shared.get("next_attempt") and now < datetime.fromisoformat(shared["next_attempt"]):
            return {"status": "cooldown"}
        slot = shared.get("request_slot", 0) + 1
        shared["request_slot"] = slot
        if self.input_collector is not None and slot % 2 == 0:
            shared["next_attempt"] = (now + timedelta(minutes=1)).isoformat()
            write_json(self.minute.state_path, shared)
            return self.input_collector.tick(now)
        write_json(self.minute.state_path, shared)
        return self.minute.tick(now)


def main() -> None:
    import logging
    from .broker import WebullSandboxBroker

    parser = argparse.ArgumentParser(description="Read-only Webull historical bar collection; no trading or data purchases")
    parser.add_argument("--root", default="research/webull")
    parser.add_argument("--symbols", required=True, help="Comma-separated explicit tickers")
    parser.add_argument("--timespan", choices=("M5", "D"), default="M5")
    parser.add_argument("--requests", type=int, default=1)
    parser.add_argument("--retry-blocked", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.requests <= 5:
        parser.error("request budget must be 1 through 5")
    logging.disable(logging.CRITICAL)
    broker = WebullSandboxBroker.from_env().connect()
    if not broker.sandbox_mode or broker.live_mode:
        raise ValueError("historical collection uses sandbox credentials only")
    collector = WebullHistoryCollector(args.root, broker.session.data_client.market_data.get_batch_history_bar, timespan=args.timespan)
    now = datetime.now(timezone.utc)
    collector.seed(args.symbols.split(","), now)
    if args.retry_blocked:
        collector.retry_blocked()
    for _ in range(args.requests):
        result = collector.tick(datetime.now(timezone.utc))
        print(result)
        if result["status"] in ("cooldown", "idle", "permission_blocked", "rate_limited", "retry", "symbol_or_query_unavailable", "storage_budget_reached"):
            break


if __name__ == "__main__":
    main()