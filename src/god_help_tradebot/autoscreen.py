"""Automatic candidate discovery using the Webull OpenAPI screener.

Pulls the sandbox/production top-gainers list, converts ranked rows into
MarketSnapshot objects, and enriches promising symbols with live bid/ask so the
strategy screener can evaluate spread and liquidity. No trading action is taken here.
"""

from __future__ import annotations

from typing import Any, Iterable

from .broker import WebullSandboxBroker, US_STOCK_CATEGORY
from .config import BotConfig
from .paper import Quote
from .screen import MarketSnapshot

GAINER_RANK_TYPE = "DAY_1"
GAINER_SORT = "CHANGE_RATIO"
DESCENDING = "DESC"


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
    )


def _change_percent(row: dict[str, Any]) -> float:
    """change_ratio arrives as a decimal fraction (1.98 == ~198%)."""
    return _decimal_or_zero(row.get("change_ratio")) * 100


def fetch_top_gainers(broker: WebullSandboxBroker, limit: int = 50) -> list[dict[str, Any]]:
    """Fetch the top gainers list from the broker's data client."""
    response = broker.session.data_client.screener.list_gainers_losers(
        rank_type=GAINER_RANK_TYPE,
        category=US_STOCK_CATEGORY,
        sort_by=GAINER_SORT,
        direction=DESCENDING,
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

    A batch that hits a rate limit or other API error is skipped rather than
    failing the whole enrichment pass.
    """
    candidates = list(snapshots)
    quotes: dict[str, Quote] = {}
    for start in range(0, len(candidates), 100):
        batch = candidates[start : start + 100]
        try:
            quotes.update(broker.get_latest_quotes([s.symbol for s in batch]))
        except Exception:
            continue

    enriched: list[MarketSnapshot] = []
    for snapshot in candidates:
        quote = quotes.get(snapshot.symbol)
        if quote is None or quote.bid <= 0 or quote.ask <= 0:
            continue
        enriched.append(
            MarketSnapshot(
                symbol=snapshot.symbol,
                price=snapshot.price,
                change_percent=snapshot.change_percent,
                volume=snapshot.volume,
                average_volume=snapshot.average_volume,
                bid=float(quote.bid),
                ask=float(quote.ask),
                day_low=snapshot.day_low,
                day_high=snapshot.day_high,
            )
        )
    return enriched


def auto_screen_snapshots(
    broker: WebullSandboxBroker,
    config: BotConfig | None = None,
    *,
    limit: int = 50,
) -> list[MarketSnapshot]:
    """Fetch top gainers, convert to snapshots, pre-filter on price, enrich with quotes.

    Symbols above the configured max price are skipped before quote enrichment to
    avoid wasting API calls.
    """
    config = config or BotConfig()
    rows = fetch_top_gainers(broker, limit=limit)
    snapshots = []
    for row in rows:
        snapshot = screener_row_to_snapshot(row)
        if snapshot is None:
            continue
        if snapshot.price > config.max_symbol_price:
            continue
        if snapshot.change_percent < config.min_change_percent:
            continue
        snapshots.append(snapshot)
    return enrich_with_quotes(broker, snapshots)


__all__ = [
    "auto_screen_snapshots",
    "enrich_with_quotes",
    "fetch_top_gainers",
    "screener_row_to_snapshot",
]
