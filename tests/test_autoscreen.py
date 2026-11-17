from decimal import Decimal

import pytest

from god_help_tradebot import BotConfig, WebullSdkSession, WebullSandboxBroker
from god_help_tradebot.autoscreen import (
    auto_screen_snapshots,
    enrich_with_quotes,
    fetch_top_gainers,
    screener_row_to_snapshot,
)
from god_help_tradebot.paper import Quote
from god_help_tradebot.screen import MarketSnapshot


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class _InvalidSymbolError(RuntimeError):
    http_status = 417
    error_code = "INVALID_SYMBOL"


class _FakeScreener:
    def __init__(self, rows) -> None:
        self._rows = rows

    def list_gainers_losers(self, rank_type, category, sort_by, direction=None):
        return _FakeResponse(self._rows)


class _FakeMarketData:
    def __init__(self, quotes: dict[str, Quote]) -> None:
        self._quotes = quotes
        self.batch_calls = 0

    def get_snapshot(self, symbols, category):
        tickers = [symbols] if isinstance(symbols, str) else list(symbols)
        self.batch_calls += 1
        rows = []
        for ticker in tickers:
            quote = self._quotes.get(ticker.upper())
            if quote is not None:
                rows.append({
                    "symbol": quote.symbol,
                    "price": str(quote.last),
                    "bid": str(quote.bid),
                    "ask": str(quote.ask),
                })
        return _FakeResponse(rows)


class _FakeDataClient:
    def __init__(self, rows, quotes: dict[str, Quote]) -> None:
        self.screener = _FakeScreener(rows)
        self.market_data = _FakeMarketData(quotes)


def _broker(rows, quotes) -> WebullSandboxBroker:
    session = WebullSdkSession(
        trade_client=None,
        data_client=_FakeDataClient(rows, quotes),
        account_id="cfg-1",
    )
    return WebullSandboxBroker(session=session)


def _row(symbol: str, price: float, change_ratio: float, volume: int, rel_vol: float, low: float, high: float):
    return {
        "symbol": symbol,
        "price": str(price),
        "close": str(price),
        "change_ratio": change_ratio,
        "volume": str(volume),
        "relative_volume_10d": str(rel_vol),
        "low": str(low),
        "high": str(high),
    }


def test_screener_row_to_snapshot_converts_ratio_to_percent() -> None:
    snapshot = screener_row_to_snapshot(_row("GOW", 3.18, 1.98, 500_000, 2.5, 2.80, 3.40))

    assert snapshot.symbol == "GOW"
    assert snapshot.price == 3.18
    assert snapshot.change_percent == 198.0
    assert snapshot.volume == 500_000
    assert snapshot.average_volume == 200_000
    assert snapshot.relative_volume == 2.5
    assert snapshot.day_low == 2.80
    assert snapshot.day_high == 3.40


def test_account_price_priority_discovers_stocks_above_twenty_dollars():
    rows = [_row("HIGH", 300, 0.05, 500_000, 2.5, 290, 310)]
    broker = _broker(rows, {"HIGH": Quote("HIGH", Decimal("299.99"), Decimal("300.01"), Decimal("300"))})
    assert auto_screen_snapshots(broker, BotConfig()) == []
    result = auto_screen_snapshots(broker, BotConfig(account_price_priority=True))
    assert result[0].symbol == "HIGH"


def test_screener_row_to_snapshot_rejects_bad_rows() -> None:
    assert screener_row_to_snapshot({"symbol": "", "price": "1.0"}) is None
    assert screener_row_to_snapshot(_row("GOW", 0, 1.0, 1, 1.0, 0.9, 1.1)) is None


def test_fetch_top_gainers_returns_rows_from_broker() -> None:
    rows = [_row("GOW", 3.18, 1.98, 500_000, 2.5, 2.80, 3.40)]
    broker = _broker(rows, {})

    assert fetch_top_gainers(broker, limit=10) == rows


def test_enrich_with_quotes_replaces_placeholder_bid_ask() -> None:
    quote = Quote("GOW", Decimal("3.10"), Decimal("3.12"), Decimal("3.11"))
    broker = _broker([], {"GOW": quote})
    snapshot = screener_row_to_snapshot(_row("GOW", 3.18, 1.98, 500_000, 2.5, 2.80, 3.40))

    enriched = enrich_with_quotes(broker, [snapshot])

    assert enriched[0].bid == 3.10
    assert enriched[0].ask == 3.12


def test_enrich_with_quotes_drops_symbols_without_quotes() -> None:
    broker = _broker([], {})
    snapshot = screener_row_to_snapshot(_row("GOW", 3.18, 1.98, 500_000, 2.5, 2.80, 3.40))

    assert enrich_with_quotes(broker, [snapshot]) == []


def test_invalid_symbol_does_not_discard_valid_members_of_quote_batch():
    quotes = {
        "GOOD1": Quote("GOOD1", Decimal("2.00"), Decimal("2.01"), Decimal("2.00")),
        "GOOD2": Quote("GOOD2", Decimal("3.00"), Decimal("3.01"), Decimal("3.00")),
    }
    broker = _broker([], quotes)
    calls = []

    def get_latest_quotes(symbols):
        calls.append(list(symbols))
        if "BAD" in symbols:
            raise _InvalidSymbolError("INVALID_SYMBOL: The symbols does not exist in the category. [BAD].")
        return {symbol: quotes[symbol] for symbol in symbols if symbol in quotes}

    broker.get_latest_quotes = get_latest_quotes
    snapshots = [screener_row_to_snapshot(_row(symbol, price, 0.05, 500_000, 2.5, price * 0.9, price * 1.1))
                 for symbol, price in (("GOOD1", 2.0), ("BAD", 2.5), ("GOOD2", 3.0))]
    enriched = enrich_with_quotes(broker, snapshots)
    assert [snapshot.symbol for snapshot in enriched] == ["GOOD1", "GOOD2"]
    assert calls == [["GOOD1", "BAD", "GOOD2"], ["GOOD1", "GOOD2"]]
    assert broker._invalid_quote_symbols == {"BAD"}


def test_rate_limit_aborts_quote_batches_for_backoff():
    class RateLimitError(RuntimeError):
        http_status = 429

    broker = _broker([], {})
    calls = []

    def limited(symbols):
        calls.append(list(symbols))
        raise RateLimitError("TOO_MANY_REQUESTS")

    broker.get_latest_quotes = limited
    snapshots = [screener_row_to_snapshot(_row("GOOD", 2.0, 0.05, 500_000, 2.5, 1.9, 2.1))]
    with pytest.raises(RateLimitError):
        enrich_with_quotes(broker, snapshots)
    assert len(calls) == 1


def test_invalid_symbol_retries_are_limited_to_one_per_batch():
    broker = _broker([], {})
    calls = []

    def get_latest_quotes(symbols):
        calls.append(list(symbols))
        raise _InvalidSymbolError(
            f"INVALID_SYMBOL: The symbols does not exist in the category. [{symbols[0]}]."
        )

    broker.get_latest_quotes = get_latest_quotes
    snapshots = [
        screener_row_to_snapshot(_row(f"BAD{index}", 2.0, 0.05, 500_000, 2.5, 1.9, 2.1))
        for index in range(400)
    ]

    assert enrich_with_quotes(broker, snapshots) == []
    assert len(calls) == 8
    assert all(len(batch) <= 100 for batch in calls)


def test_auto_screen_filters_price_and_change_before_enrichment() -> None:
    rows = [
        _row("TOOEXPENSIVE", 20.01, 1.0, 500_000, 2.5, 19.0, 21.0),
        _row("TOOCHEAP", 0.99, 1.0, 500_000, 2.5, 0.90, 1.05),
        _row("TOOLITTLEMOVE", 2.00, 0.01, 500_000, 2.5, 1.90, 2.10),
        _row("GOW", 3.18, 0.10, 500_000, 2.5, 2.80, 3.40),
    ]
    quote = Quote("GOW", Decimal("3.10"), Decimal("3.12"), Decimal("3.11"))
    broker = _broker(rows, {"GOW": quote})
    config = BotConfig(min_change_percent=2.0)

    snapshots = auto_screen_snapshots(broker, config)

    assert [s.symbol for s in snapshots] == ["GOW"]
    assert snapshots[0].bid == 3.10


def test_auto_screen_enriches_in_one_batched_quote_call() -> None:
    rows = [
        _row("GOW", 3.18, 0.10, 500_000, 2.5, 2.80, 3.40),
        _row("TNON", 4.09, 0.05, 900_000, 3.0, 3.80, 4.30),
    ]
    quotes = {
        "GOW": Quote("GOW", Decimal("3.10"), Decimal("3.12"), Decimal("3.11")),
        "TNON": Quote("TNON", Decimal("4.05"), Decimal("4.07"), Decimal("4.06")),
    }
    broker = _broker(rows, quotes)
    config = BotConfig(max_symbol_price=10.0, min_change_percent=2.0)

    snapshots = auto_screen_snapshots(broker, config)

    assert [s.symbol for s in snapshots] == ["GOW", "TNON"]
    assert broker.session.data_client.market_data.batch_calls == 1
