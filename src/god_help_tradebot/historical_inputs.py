from __future__ import annotations

import hashlib
import json
import math
from collections import deque
from bisect import bisect_right
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .orderflow import market_time
from .patterns import Bar, detect_pattern, detect_bear_pattern
from .research import read_json, write_json
from .broker import US_STOCK_CATEGORY, is_rate_limit_error


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def known_time(row: dict[str, Any], retrieved_at: datetime) -> datetime:
    for name in ("available_at", "asof", "timestamp", "time"):
        stamp = market_time(row.get(name))
        if stamp is not None:
            return stamp
    return retrieved_at


class HistoricalInputArchive:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._loaded = {}

    def ingest(self, symbol: str, kind: str, rows: list[dict[str, Any]], retrieved_at: datetime,
               *, source: str = "verified import") -> int:
        if kind not in ("news", "capital_flow", "analysts", "orderflow", "corporate_actions", "quotes", "borrow", "universe"):
            raise ValueError("unsupported historical input kind")
        if not symbol or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-" for character in symbol):
            raise ValueError("invalid historical input symbol")
        if retrieved_at.tzinfo is None:
            raise ValueError("historical retrieval time must be timezone-aware")
        path = self.root / kind / f"{symbol}.json"
        if path.exists() and path.stat().st_size >= 16 * 1024 * 1024:
            raise ValueError("per-symbol input archive exceeds the bounded in-memory limit")
        previous = read_json(path, {"source": source, "records": []})
        saved = {record["id"]: record for record in previous["records"]}
        count = 0
        for row in rows:
            available = known_time(row, retrieved_at)
            if available > retrieved_at:
                continue
            record = {"source": source, "available_at": available.isoformat(), "retrieved_at": retrieved_at.isoformat(), "symbol": symbol, "kind": kind}
            if kind == "news":
                publication = market_time(row.get("publish_time"))
                if publication is None or publication > retrieved_at:
                    continue
                record.update(news_id=row.get("news_id"), title=row.get("title"), publish_time=publication.isoformat(),
                              sentiment=row.get("sentiment"), sentiment_available_at=known_time(
                                  {"available_at": row.get("sentiment_asof")}, retrieved_at).isoformat())
                record["available_at"] = publication.isoformat()
            else:
                allowed = {
                    "capital_flow": ("large_in", "large_out", "medium_in", "medium_out", "small_in", "small_out"),
                    "analysts": ("buy", "strong_buy", "hold", "sell", "strong_sell", "underperform", "under_perform"),
                    "orderflow": ("buy_total", "sell_total", "interval_seconds"),
                    "corporate_actions": ("type", "ratio", "amount", "effective_at"),
                    "quotes": ("bid", "ask", "last", "bid_volume", "ask_volume", "reference_price_basis"),
                    "borrow": ("shortable",), "universe": ("listed_at", "delisted_at"),
                }[kind]
                record.update({name: row[name] for name in allowed if name in row})
                if kind == "corporate_actions":
                    field = "ratio" if record.get("type") == "split" else "amount" if record.get("type") == "dividend" else None
                    value = finite_number(record.get(field)) if field else None
                    if field is None or value is None or value <= 0 or market_time(record.get("effective_at")) is None:
                        continue
                if kind == "orderflow":
                    values = [finite_number(record.get(name)) for name in ("buy_total", "sell_total")]
                    if any(value is None or value < 0 for value in values) or sum(values) <= 0:
                        continue
                if kind == "borrow" and type(record.get("shortable")) is not bool:
                    continue
            identity = record
            if kind == "news":
                identity = {name: record.get(name) for name in ("symbol", "kind", "source", "news_id", "publish_time", "sentiment", "title")}
            record["id"] = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
            if record["id"] not in saved:
                saved[record["id"]] = record
                count += 1
        write_json(path, {"source": source, "records": sorted(saved.values(), key=lambda record: record["available_at"])})
        self._loaded.pop((symbol, kind), None)
        return count

    def at(self, symbol: str, kind: str, when: datetime) -> list[dict[str, Any]]:
        key = (symbol, kind)
        if key not in self._loaded:
            self._loaded[key] = read_json(self.root / kind / f"{symbol}.json", {"records": []})["records"]
        records = self._loaded[key]
        return [record for record in records if datetime.fromisoformat(record["available_at"]) <= when]

    def features(self, symbol: str, bars: list[Bar], when: datetime) -> dict[str, Any]:
        features: dict[str, Any] = {"asof": when.isoformat(), "symbol": symbol, "sources": {}, "missing": []}
        bars = [bar for bar in bars if (stamp := market_time(bar.time)) is not None and stamp + timedelta(minutes=5) <= when]
        pattern = detect_pattern(bars, symbol)
        bearish = detect_bear_pattern(bars, symbol)
        if pattern.has_data and pattern.pattern is not None:
            features.update(pattern_score=pattern.score, pattern_name=pattern.pattern)
            features["sources"]["patterns"] = "reconstructed from completed unadjusted bars"
        if bearish.has_data and bearish.pattern is not None:
            features.update(bear_pattern_score=bearish.score, bear_pattern_name=bearish.pattern)
        flows = self.at(symbol, "capital_flow", when)
        if flows:
            row = flows[-1]
            values = {name: finite_number(row.get(name)) for name in ("large_in", "large_out", "medium_in", "medium_out", "small_in", "small_out")}
            if all(value is not None and value >= 0 for value in values.values()) and values["large_in"] + values["large_out"] > 0:
                features["large_flow_ratio"] = values["large_in"] / (values["large_in"] + values["large_out"])
                features["net_capital_flow"] = sum(values[name] for name in ("large_in", "medium_in", "small_in")) - sum(values[name] for name in ("large_out", "medium_out", "small_out"))
                features["sources"]["capital_flow"] = row["source"]
        ratings = self.at(symbol, "analysts", when)
        if ratings:
            row = ratings[-1]
            values = [finite_number(row.get(name, 0)) for name in ("buy", "strong_buy", "hold", "sell", "strong_sell", "underperform", "under_perform")]
            if all(value is not None and value >= 0 for value in values) and sum(values) > 0:
                features["analyst_buy_ratio"] = sum(values[:2]) / sum(values)
                features["sources"]["analysts"] = row["source"]
        available_news = [row for row in self.at(symbol, "news", when) if datetime.fromisoformat(row["publish_time"]) >= when - timedelta(hours=72)]
        news = list({(row.get("news_id"), row["publish_time"]): row for row in available_news}.values())
        sentiment = {"POSITIVE": 1.0, "NEGATIVE": 0.0, "NEUTRAL": 0.5}
        scored = [sentiment[row["sentiment"]] for row in news if row.get("sentiment") in sentiment and datetime.fromisoformat(row["sentiment_available_at"]) <= when]
        features["news_article_count"] = len(news)
        if scored:
            features["news_score"] = sum(scored) / len(scored)
            features["sources"]["news"] = "dated archive with explicitly available sentiment"
        footprints = self.at(symbol, "orderflow", when)
        if footprints:
            row = footprints[-1]
            buy, sell = finite_number(row.get("buy_total")), finite_number(row.get("sell_total"))
            age = (when - datetime.fromisoformat(row["available_at"])).total_seconds()
            if buy is not None and sell is not None and buy >= 0 and sell >= 0 and buy + sell > 0 and 0 <= age <= 60:
                features["orderflow_score"] = buy / (buy + sell)
                features["sources"]["orderflow"] = row["source"]
        for group, field in (("capital_flow", "large_flow_ratio"), ("analysts", "analyst_buy_ratio"), ("news", "news_score"), ("orderflow", "orderflow_score")):
            if field not in features:
                features["missing"].append(group)
        return features

    def export_bar_features(self, bar_root: Path, output: Path, *, now: datetime) -> int:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        count = 0
        with temporary.open("w", encoding="utf-8") as handle:
            for directory in sorted(bar_root.glob("*-M5")):
                symbol = directory.name[:-3]
                history = deque(maxlen=30)
                previous_time = None
                for path in sorted(directory.glob("*.json"), key=lambda path: int(path.stem)):
                    page = read_json(path)
                    if page.get("adjustment") != "unadjusted":
                        continue
                    for row in sorted(page.get("bars", []), key=lambda row: row["time"]):
                        stamp = market_time(row.get("time"))
                        if stamp is None or (previous_time is not None and stamp <= previous_time):
                            continue
                        completed_at = stamp + timedelta(minutes=5)
                        if completed_at > now:
                            continue
                        bar = Bar(row["time"], float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row["volume"]))
                        history.append(bar)
                        previous_time = stamp
                        features = self.features(symbol, list(history), completed_at)
                        handle.write(json.dumps({"time": completed_at.isoformat(), "historical_features": {symbol: features}}, allow_nan=False) + "\n")
                        count += 1
        temporary.replace(output)
        write_json(output.with_suffix(".coverage.json"), {"feature_rows": count, "source": "completed Webull M5 bars and dated input archive",
            "executable_quotes_synthesized": False, "promotion_eligible": False,
            "remaining_requirements": ["verified historical quote/depth archive", "complete point-in-time universe and borrow", "source-matched historical confirmation signals"]})
        return count

    def overlay_steps(self, steps: list[dict[str, Any]], feature_file: Path) -> int:
        timelines = {}
        if feature_file.exists():
            with feature_file.open(encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    stamp = datetime.fromisoformat(row["time"])
                    for symbol, features in row.get("historical_features", {}).items():
                        timelines.setdefault(symbol, []).append((stamp, features))
        indexed = {symbol: (sorted(values), sorted(stamp for stamp, _ in values)) for symbol, values in timelines.items()}
        augmented = 0
        previous_time = None
        for step in steps:
            when = datetime.fromisoformat(step["time"])
            symbols = {snapshot["symbol"] for snapshot in step.get("snapshots", [])} | set(step.get("confirmations", {}))
            for symbol in symbols:
                quote_symbols = {quote["symbol"] for quote in step.get("quotes", [])}
                quotes = self.at(symbol, "quotes", when)
                if symbol not in quote_symbols and quotes:
                    quote = quotes[-1]
                    values = [finite_number(quote.get(field)) for field in ("bid", "ask", "last", "bid_volume", "ask_volume")]
                    age = (when - datetime.fromisoformat(quote["available_at"])).total_seconds()
                    if 0 <= age <= 30 and all(value is not None and value > 0 for value in values) and values[0] <= values[1]:
                        step.setdefault("quotes", []).append({"symbol": symbol, **{field: quote[field] for field in ("bid", "ask", "last", "bid_volume", "ask_volume")}})
                        augmented += 1
                borrow = self.at(symbol, "borrow", when)
                if symbol not in step.get("shortable", {}) and borrow:
                    row = borrow[-1]
                    if type(row.get("shortable")) is bool and (when - datetime.fromisoformat(row["available_at"])).total_seconds() <= 86400:
                        step.setdefault("shortable", {})[symbol] = row["shortable"]
                for action in self.at(symbol, "corporate_actions", when):
                    effective = market_time(action.get("effective_at"))
                    if effective is None or effective > when or (previous_time is not None and effective <= previous_time):
                        continue
                    field = "ratio" if action.get("type") == "split" else "amount" if action.get("type") == "dividend" else None
                    value = finite_number(action.get(field)) if field else None
                    if field and value is not None and value > 0:
                        event = {"symbol": symbol, "type": action["type"], field: action[field]}
                        if event not in step.setdefault("corporate_actions", []):
                            step["corporate_actions"].append(event)
                values, stamps = indexed.get(symbol, ([], []))
                index = bisect_right(stamps, when) - 1
                if index < 0 or (when - stamps[index]).total_seconds() > 300:
                    continue
                feature = values[index][1]
                sources = feature["sources"]
                for direction in ("bullish", "bearish"):
                    report = step.setdefault("confirmations", {}).setdefault(symbol, {}).get(direction)
                    if report is not None:
                        for field in ("large_flow_ratio", "analyst_buy_ratio", "pattern_score", "pattern_name"):
                            feature_field = "bear_" + field if direction == "bearish" and field.startswith("pattern_") else field
                            if field not in report and feature_field in feature:
                                report[field] = feature[feature_field]
                                augmented += 1
                        continue
                    bearish = direction == "bearish"
                    flow = feature.get("large_flow_ratio")
                    rating = feature.get("analyst_buy_ratio")
                    pattern = feature.get("bear_pattern_score" if bearish else "pattern_score")
                    weighted = [(1 - flow if bearish and flow is not None else flow if flow is not None else 0.5, 0.45),
                                (1 - rating if bearish and rating is not None else rating if rating is not None else 0.5, 0.20)]
                    if pattern is not None:
                        weighted.append((pattern, 0.15))
                    score = sum(value * weight for value, weight in weighted) / sum(weight for _, weight in weighted)
                    report = {"asof": when.isoformat(), "score": score, "passed": score >= 0.5,
                              "signals_present": sum(value is not None for value in (flow, rating, pattern)),
                              "reconstructed": True, "sources": sources,
                              "missing": feature["missing"], "pattern_score": pattern}
                    if flow is not None:
                        report["large_flow_ratio"] = flow
                    if rating is not None:
                        report["analyst_buy_ratio"] = rating
                    step["confirmations"][symbol][direction] = report
                    augmented += 1
                step.setdefault("historical_features", {})[symbol] = feature
            previous_time = when
        return augmented


class WebullInputCollector:
    def __init__(self, root: Path, data_client: Any) -> None:
        self.root = root
        self.client = data_client
        self.archive = HistoricalInputArchive(root / "archive")

    def seed(self, symbols: list[str]) -> None:
        state = read_json(self.root / "collection.json", {"series": {}, "offset": 0})
        for symbol in symbols[:20]:
            for kind in ("news", "capital_flow", "analysts", "orderflow", "quotes", "corporate_actions"):
                if f"{symbol}:{kind}" not in state["series"] and len(state["series"]) >= 600:
                    continue
                state["series"].setdefault(f"{symbol}:{kind}", {"symbol": symbol, "kind": kind, "status": "pending", "records": 0})
        write_json(self.root / "collection.json", state)

    def request(self, item: dict[str, Any]) -> Any:
        symbol, kind = item["symbol"], item["kind"]
        if kind == "capital_flow":
            return self.client.fundamentals.get_capital_flow(symbol, US_STOCK_CATEGORY, count=5)
        if kind == "analysts":
            return self.client.instrument.get_analyst_rating(symbol, US_STOCK_CATEGORY)
        if kind == "orderflow":
            return self.client.market_data.get_footprint([symbol], US_STOCK_CATEGORY, "M1", count="1200", real_time_required=False,
                                                        trading_sessions=["PRE", "RTH", "ATH"])
        if kind == "quotes":
            return self.client.market_data.get_quotes(symbol, US_STOCK_CATEGORY, depth=1, overnight_required=True)
        from webull.core.request import ApiRequest
        path = "/market-data/news/stock-news/list" if kind == "news" else "/market-data/instruments/stocks/corporate-actions/list"
        parameters = {"symbol": symbol, "category": US_STOCK_CATEGORY}
        if item.get("pagination_key"):
            parameters["pagination_key"] = item["pagination_key"]
        return self.client.market_data.client.get_response(ApiRequest(path, version="v3", method="GET", query_params=parameters))

    def tick(self, now: datetime) -> dict[str, Any]:
        from .orderflow import records

        path = self.root / "collection.json"
        state = read_json(path, {"series": {}, "offset": 0})
        if state.get("next_attempt") and now < datetime.fromisoformat(state["next_attempt"]):
            return {"status": "cooldown"}
        candidates = [(key, item) for key, item in state["series"].items() if item["status"] in ("pending", "paging", "retry")
                      or (item.get("refresh_at") and now >= datetime.fromisoformat(item["refresh_at"]) and item["status"] == "observed")]
        if not candidates:
            return {"status": "idle"}
        key, item = candidates[state["offset"] % len(candidates)]
        if sum(file.stat().st_size for file in (self.root / "archive").glob("**/*.json")) >= 512 * 1024 * 1024:
            return {"status": "input_storage_budget_reached"}
        state["offset"] += 1
        state["next_attempt"] = (now + timedelta(minutes=1)).isoformat()
        try:
            response = self.request(item)
            if response.status_code != 200:
                error = RuntimeError(f"HTTP Status: {response.status_code}")
                error.status_code = response.status_code
                raise error
            payload = response.json()
            rows = records(payload)
            normalized = []
            for row in rows:
                result = dict(row)
                if item["kind"] == "orderflow":
                    stamp = market_time(row.get("time"))
                    if stamp is None:
                        continue
                    result["available_at"] = (stamp + timedelta(minutes=1)).isoformat()
                    result["interval_seconds"] = 60
                elif item["kind"] == "analysts":
                    result["available_at"] = now.isoformat()
                elif item["kind"] == "quotes":
                    basic = row.get("basic", {})
                    bids, asks = row.get("bids", []), row.get("asks", [])
                    stamp = market_time(row.get("timestamp") or basic.get("timestamp"))
                    if not bids or not asks or stamp is None or stamp > now:
                        continue
                    result = {"available_at": stamp.isoformat(), "bid": bids[0]["price"], "ask": asks[0]["price"],
                              "bid_volume": bids[0].get("size"), "ask_volume": asks[0].get("size")}
                    result["last"] = (float(result["bid"]) + float(result["ask"])) / 2
                    result["reference_price_basis"] = "quoted_midpoint_not_trade_price"
                normalized.append(result)
            added = self.archive.ingest(item["symbol"], item["kind"], normalized, now, source=f"Webull OpenAPI:{item['kind']}")
            item["records"] += added
            next_key = payload.get("pagination_key") if isinstance(payload, dict) else None
            seen = item.setdefault("seen_cursors", [])
            if next_key and (next_key == item.get("pagination_key") or next_key in seen):
                item["status"] = "no_progress"
            elif next_key and item["kind"] in ("news", "corporate_actions"):
                if len(seen) >= 200:
                    item["status"] = "pagination_budget_reached"
                else:
                    seen.append(next_key)
                    item["pagination_key"] = next_key
                    item["status"] = "paging"
            else:
                item["status"] = "observed"
                item["refresh_at"] = (now + timedelta(days=1)).isoformat()
                if item["kind"] == "news":
                    item.pop("pagination_key", None)
                    item["seen_cursors"] = []
            item["scope"] = "current_observation_only" if item["kind"] in ("analysts", "quotes") else "timestamped_available_records"
            item["last_new_records"] = added
        except Exception as error:
            status = str(getattr(error, "http_status", getattr(error, "status_code", "")))
            item["status"] = "permission_blocked" if status in ("401", "403") else "unsupported_or_unavailable" if status in ("404", "417") else "retry"
            if is_rate_limit_error(error):
                item["status"] = "retry"
            state["next_attempt"] = (now + timedelta(minutes=5)).isoformat()
            item["reason"] = type(error).__name__
        write_json(path, state)
        write_json(self.root / "coverage.json", {"series": state["series"], "checked_at": now.isoformat(),
            "missing_external_history": ["complete historical best bid/ask depth", "point-in-time delisted universe", "historical borrow availability"],
            "remaining_provider_limits": {"capital_flow": "API returns at most five distribution records", "analysts": "latest ratings are not historical ratings",
                                          "orderflow": "footprints require entitlement, have no cursor in installed SDK, and do not support overnight",
                                          "news": "publication date does not prove historical sentiment availability"},
            "no_subscriptions_purchased": True, "current_observations_are_not_backdated": True})
        return {"symbol": item["symbol"], "kind": item["kind"], "status": item["status"], "records": item["records"]}


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Import dated research inputs without API credentials or account orders")
    parser.add_argument("--root", default="research/webull/inputs/archive")
    parser.add_argument("--import-file", required=True)
    args = parser.parse_args()
    payload = read_json(Path(args.import_file))
    if not isinstance(payload, dict) or not isinstance(payload.get("records"), list) or not payload.get("source"):
        parser.error("input requires symbol, kind, retrieved_at, source, and records")
    retrieved = market_time(payload.get("retrieved_at"))
    if retrieved is None:
        parser.error("retrieved_at must be timezone-aware")
    archive = HistoricalInputArchive(args.root)
    count = archive.ingest(payload["symbol"].upper(), payload["kind"], payload["records"], retrieved, source=payload["source"])
    print({"records_imported": count, "kind": payload["kind"], "symbol": payload["symbol"], "automatic_promotion": False})


if __name__ == "__main__":
    main()