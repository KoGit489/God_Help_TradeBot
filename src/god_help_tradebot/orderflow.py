from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from .broker import US_STOCK_CATEGORY, is_rate_limit_error
from .schedule import EASTERN, equity_order_session


def market_time(value: Any) -> datetime | None:
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            number = float(value)
            if number > 100_000_000_000:
                number /= 1000
            return datetime.fromtimestamp(number, EASTERN)
        timestamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return timestamp.astimezone(EASTERN) if timestamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("result", "data"):
            if key in payload:
                return records(payload[key])
        return [payload]
    if isinstance(payload, list):
        return [record for item in payload for record in records(item)]
    return []


def orderflow_score(broker: Any, symbol: str, now: datetime | None = None) -> float | None:
    now = now or datetime.now(EASTERN)
    try:
        response = broker.session.data_client.market_data.get_tick(
            symbol, US_STOCK_CATEGORY, count="200", trading_sessions=["PRE", "RTH", "POST", "OVN"],
        )
        if response.status_code != 200:
            if response.status_code == 429:
                raise RuntimeError("HTTP Status: 429; orderflow rate limited")
            return None
        buy = Decimal("0")
        sell = Decimal("0")
        for row in records(response.json()):
            timestamp = market_time(row.get("time"))
            if timestamp is None or not 0 <= (now - timestamp).total_seconds() <= 60:
                continue
            size = Decimal(str(row.get("volume", 0)))
            if not size.is_finite() or size <= 0:
                continue
            side = str(row.get("side", "")).upper()
            if side in ("BUY", "B"):
                buy += size
            elif side in ("SELL", "S"):
                sell += size
        return float(buy / (buy + sell)) if buy + sell > 0 else None
    except Exception as error:
        if is_rate_limit_error(error):
            raise
        return None


def full_day_extremes(broker: Any, symbol: str, day: date) -> tuple[Decimal, Decimal] | None:
    from .patterns import bars_from_response

    start = datetime.combine(day, time(), EASTERN)
    end = start + timedelta(days=1)
    response = broker.session.data_client.market_data.get_batch_history_bar(
        [symbol], US_STOCK_CATEGORY, "m5", count="1200", real_time_required=True,
        trading_sessions=["PRE", "RTH", "POST", "OVN"],
        start_time=int(start.timestamp() * 1000), end_time=int(end.timestamp() * 1000) - 1,
    )
    if response.status_code != 200:
        if response.status_code == 429:
            raise RuntimeError("HTTP Status: 429; historical data rate limited")
        return None
    bars = bars_from_response(response.json())
    valid = [bar for bar in bars if (stamp := market_time(bar.time)) is not None and start <= stamp < end
             and all(Decimal(str(value)).is_finite() and value > 0 for value in (bar.high, bar.low))
             and bar.low <= bar.high]
    if not valid or len(valid) != len(bars):
        return None
    covered = {market_time(bar.time).astimezone(EASTERN).replace(second=0, microsecond=0) for bar in valid}
    expected = set()
    timestamp = start
    while timestamp < end:
        if equity_order_session(timestamp) != "CLOSED":
            expected.add(timestamp)
        timestamp += timedelta(minutes=5)
    if not expected or not expected <= covered:
        return None
    return Decimal(str(max(bar.high for bar in valid))), Decimal(str(min(bar.low for bar in valid)))