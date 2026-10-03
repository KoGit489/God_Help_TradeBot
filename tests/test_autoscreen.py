from decimal import Decimal

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


def test_auto_screen_filters_price_and_change_before_enrichment() -> None:
    rows = [
        _row("TOOEXPENSIVE", 15.00, 1.0, 500_000, 2.5, 14.0, 16.0),
        _row("TOOLITTLEMOVE", 2.00, 0.01, 500_000, 2.5, 1.90, 2.10),
        _row("GOW", 3.18, 0.10, 500_000, 2.5, 2.80, 3.40),
    ]
    quote = Quote("GOW", Decimal("3.10"), Decimal("3.12"), Decimal("3.11"))
    broker = _broker(rows, {"GOW": quote})
    config = BotConfig(max_symbol_price=10.0, min_change_percent=2.0)

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
