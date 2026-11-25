"""Automatic candidate discovery using the Webull OpenAPI screener.

Pulls the sandbox/production top-gainers list, converts ranked rows into
MarketSnapshot objects, and enriches promising symbols with live bid/ask so the
strategy screener can evaluate spread and liquidity. No trading action is taken here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from math import isfinite
from typing import Any, Iterable

from .broker import WebullSandboxBroker, US_STOCK_CATEGORY, is_rate_limit_error, webull_error_code
from .config import BotConfig
from .paper import Quote
from .screen import MarketSnapshot

GAINER_RANK_TYPE = "DAY_1"
GAINER_SORT = "CHANGE_RATIO"
DESCENDING = "DESC"
ASCENDING = "ASC"
MOST_ACTIVE_RANK_TYPE = "RELATIVE_VOLUME_10D"
SCREENER_MAX_ROWS = 200
DISCOVERY_REFRESH_SECONDS = 60
DISCOVERY_QUOTE_BATCHES = 4
DISCOVERY_MAX_QUOTE_ATTEMPTS_PER_BATCH = 2
MOVER_RANKS = ("DAY_1", "MIN_3", "MIN_5")
ACTIVITY_RANKS = ("RELATIVE_VOLUME_10D", "VOLUME", "TURNOVER", "TURNOVER_RATE", "AMPLITUDE")


@dataclass
class DiscoveryResult:
    snapshots: list[MarketSnapshot]
    unique_symbols: int
    eligible_symbols: int
    next_offset: int


def _decimal_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def screener_row_to_snapshot(row: dict[str, Any]) -> MarketSnapshot | None:
    """Convert one raw screener row into a MarketSnapshot, or None if unusable."""
    symbol = str(row.get("symbol") or "").strip().upper()
    price = _decimal_or_zero(row.get("price") or row.get("close"))
    if not symbol or price <= 0:
        return None

    volume = int(_decimal_or_zero(row.get("volume")))
    rel_volume = _decimal_or_zero(row.get("relative_volume_10d"))
    average_volume = int(volume / rel_volume) if rel_volume > 0 else volume or 1
    open_price = _decimal_or_zero(row.get("open"))
    previous_close = _decimal_or_zero(row.get("pre_close"))

    return MarketSnapshot(
        symbol=symbol,
        price=price,
        change_percent=_change_percent(row),
        volume=volume,
        average_volume=max(average_volume, 1),
        bid=price,  # placeholder; enriched later when a quote is available
        ask=price,
        day_low=_decimal_or_zero(row.get("low")) or price,
        day_high=_decimal_or_zero(row.get("high")) or price,
        open_price=open_price if isfinite(open_price) and open_price > 0 else None,
        previous_close=previous_close if isfinite(previous_close) and previous_close > 0 else None,
    )


def _change_percent(row: dict[str, Any]) -> float:
    """change_ratio arrives as a decimal fraction (1.98 == ~198%)."""
    return _decimal_or_zero(row.get("change_ratio")) * 100


# Pre-pop discovery keeps quiet movers only (accumulation before the spike).
PREPOP_MIN_CHANGE = -2.0
PREPOP_MAX_CHANGE = 4.0


def fetch_top_gainers(broker: WebullSandboxBroker, limit: int = SCREENER_MAX_ROWS, *, rank_type: str = GAINER_RANK_TYPE) -> list[dict[str, Any]]:
    """Fetch the top gainers list from the broker's data client."""
    response = broker.session.data_client.screener.list_gainers_losers(
        rank_type=rank_type,
        category=US_STOCK_CATEGORY,
        sort_by=GAINER_SORT,
        direction=DESCENDING,
    )
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows[:limit] if isinstance(row, dict)]


def fetch_most_active(broker: WebullSandboxBroker, limit: int = SCREENER_MAX_ROWS, *, rank_type: str = MOST_ACTIVE_RANK_TYPE) -> list[dict[str, Any]]:
    """Fetch the most-active list ranked by 10-day relative volume (pre-pop hunting)."""
    response = broker.session.data_client.screener.list_most_active(
        category=US_STOCK_CATEGORY,
        rank_type=rank_type,
        sort_by="TURNOVER" if rank_type == "TURNOVER_RATE" else rank_type,
        direction=DESCENDING,
    )
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows[:limit] if isinstance(row, dict)]


def fetch_top_losers(broker: WebullSandboxBroker, limit: int = SCREENER_MAX_ROWS, *, rank_type: str = GAINER_RANK_TYPE) -> list[dict[str, Any]]:
    """Fetch the top losers list from the broker's data client (bearish hunting)."""
    response = broker.session.data_client.screener.list_gainers_losers(
        rank_type=rank_type,
        category=US_STOCK_CATEGORY,
        sort_by=GAINER_SORT,
        direction=ASCENDING,
    )
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    return [row for row in rows[:limit] if isinstance(row, dict)]


def enrich_with_quotes(
    broker: WebullSandboxBroker,
    snapshots: Iterable[MarketSnapshot],
) -> list[MarketSnapshot]:
    """Replace placeholder bid/ask with real quotes, fetched in batches of 100.

    Invalid symbols are removed and their valid batchmates retried. Rate limits
    propagate to the loop so it can back off instead of hammering the API.
    """
    candidates = list(snapshots)
    quotes: dict[str, Quote] = {}
    invalid_symbols: set[str] = getattr(broker, "_invalid_quote_symbols", set())
    for start in range(0, len(candidates), 100):
        batch = candidates[start : start + 100]
        pending = [snapshot for snapshot in batch if snapshot.symbol not in invalid_symbols]
        for _ in range(DISCOVERY_MAX_QUOTE_ATTEMPTS_PER_BATCH):
            if not pending:
                break
            try:
                quotes.update(broker.get_latest_quotes([snapshot.symbol for snapshot in pending]))
                break
            except Exception as exc:
                if is_rate_limit_error(exc):
                    raise
                match = re.search(
                    r"(?:symbols?|symbol)\s+do(?:es)?\s+not\s+exist\s+in\s+the\s+category\.?\s*\[([^\]]+)\]",
                    str(exc), re.IGNORECASE,
                )
                rejected = {value.strip().upper() for value in match.group(1).split(",")} if match else set()
                newly_rejected = rejected.intersection(snapshot.symbol for snapshot in pending) - invalid_symbols
                if not newly_rejected:
                    logging.getLogger("god_help_tradebot").warning(
                        "[quote_batch_error] symbols=%s code=%s; batch skipped",
                        len(pending), webull_error_code(exc),
                    )
                    break
                invalid_symbols.update(newly_rejected)
                try:
                    setattr(broker, "_invalid_quote_symbols", invalid_symbols)
                except Exception:
                    pass
                logging.getLogger("god_help_tradebot").warning(
                    "[quote_invalid_symbols] count=%s; retrying remaining batch",
                    len(newly_rejected),
                )
                pending = [snapshot for snapshot in pending if snapshot.symbol not in newly_rejected]
        else:
            logging.getLogger("god_help_tradebot").warning("[quote_batch_error] invalid-symbol isolation limit reached")

    enriched: list[MarketSnapshot] = []
    for snapshot in candidates:
        quote = quotes.get(snapshot.symbol)
        if quote is None or quote.bid <= 0 or quote.ask <= 0:
            continue
        session_fields = {}
        if getattr(broker, "daily_peak_mode", False):
            from datetime import datetime
            from .schedule import EASTERN, equity_order_session
            session = equity_order_session(datetime.now(EASTERN))
            price = quote.overnight_price if session == "NIGHT" else quote.extended_price if session == "ALL" else quote.last
            if price is None or not price.is_finite() or price <= 0:
                continue
            change = quote.overnight_change_ratio if session == "NIGHT" else quote.extended_change_ratio if session == "ALL" else None
            volume = quote.overnight_volume if session == "NIGHT" else quote.extended_volume if session == "ALL" else None
            session_fields = {"price": float(price), "day_high": max(snapshot.day_high, float(price)),
                              "day_low": min(snapshot.day_low, float(price))}
            if change is not None and change.is_finite():
                session_fields["change_percent"] = float(change)
            if volume is not None and volume.is_finite() and volume >= 0:
                session_fields["volume"] = int(volume)
        enriched.append(replace(
            snapshot,
            bid=float(quote.bid), ask=float(quote.ask),
            overnight_price=float(quote.overnight_price) if quote.overnight_price is not None else None,
            overnight_change_ratio=float(quote.overnight_change_ratio) if quote.overnight_change_ratio is not None else None,
            overnight_volume=float(quote.overnight_volume) if quote.overnight_volume is not None else None,
            extended_price=float(quote.extended_price) if quote.extended_price is not None else None,
            extended_change_ratio=float(quote.extended_change_ratio) if quote.extended_change_ratio is not None else None,
            extended_volume=float(quote.extended_volume) if quote.extended_volume is not None else None,
            **session_fields,
        ))
    return enriched


def auto_screen_snapshots(
    broker: WebullSandboxBroker,
    config: BotConfig | None = None,
    *,
    limit: int = 50,
    source: str = "gainers",
) -> list[MarketSnapshot]:
    """Fetch candidates, convert to snapshots, pre-filter, enrich with quotes.

    source='gainers' hunts momentum (already moving); source='most_active' hunts
    pre-pop accumulation (unusual relative volume before the move); source='losers'
    hunts bearish momentum (already sliding).
    """
    config = config or BotConfig()
    if source == "most_active":
        rows = fetch_most_active(broker, limit=limit)
    elif source == "losers":
        rows = fetch_top_losers(broker, limit=limit)
    else:
        rows = fetch_top_gainers(broker, limit=limit)
    snapshots = []
    for row in rows:
        snapshot = screener_row_to_snapshot(row)
        if snapshot is None:
            continue
        if not config.accepts_price(snapshot.price):
            continue
        # Pre-pop discovery keeps quiet movers; momentum needs the move up;
        # losers need the move down.
        if source == "most_active":
            if not (PREPOP_MIN_CHANGE <= snapshot.change_percent <= PREPOP_MAX_CHANGE):
                continue
        elif source == "losers":
            if snapshot.change_percent > -config.min_change_percent:
                continue
        elif snapshot.change_percent < config.min_change_percent:
            continue
        snapshots.append(snapshot)
    return enrich_with_quotes(broker, snapshots)


def discover_market_snapshots(
    broker: WebullSandboxBroker,
    config: BotConfig,
    *,
    allow_shorts: bool,
    quote_offset: int = 0,
) -> DiscoveryResult:
    """Union supported rankings and rotate bounded quote batches after deduplication."""
    sources = [(fetch_top_gainers, rank) for rank in MOVER_RANKS]
    if allow_shorts:
        sources.extend((fetch_top_losers, rank) for rank in MOVER_RANKS)
    sources.extend((fetch_most_active, rank) for rank in ACTIVITY_RANKS)
    seen: dict[str, MarketSnapshot] = {}
    for fetch, rank in sources:
        try:
            rows = fetch(broker, limit=config.screener_list_limit, rank_type=rank)
        except Exception as exc:
            if is_rate_limit_error(exc):
                raise
            logging.getLogger("god_help_tradebot").warning(
                "[scan_source_error] %s %s: %s", fetch.__name__, rank, webull_error_code(exc)
            )
            continue
        for row in rows:
            snapshot = screener_row_to_snapshot(row)
            if snapshot is not None:
                seen.setdefault(snapshot.symbol, snapshot)
    eligible = sorted(
        (snapshot for snapshot in seen.values()
         if config.accepts_price(snapshot.price)),
        key=lambda snapshot: snapshot.symbol,
    )
    if not eligible:
        return DiscoveryResult([], len(seen), 0, 0)
    offset = quote_offset % len(eligible)
    rotated = eligible[offset:] + eligible[:offset]
    selected = rotated[:DISCOVERY_QUOTE_BATCHES * 100]
    snapshots = enrich_with_quotes(broker, selected)
    return DiscoveryResult(snapshots, len(seen), len(eligible), (offset + len(selected)) % len(eligible))


__all__ = [
    "auto_screen_snapshots",
    "enrich_with_quotes",
    "fetch_most_active",
    "fetch_top_gainers",
    "fetch_top_losers",
    "screener_row_to_snapshot",
]
