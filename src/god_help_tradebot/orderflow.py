from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from .broker import US_STOCK_CATEGORY, is_rate_limit_error
from .schedule import EASTERN


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
            symbol, US_STOCK_CATEGORY, count="200", trading_sessions=["PRE", "RTH", "ATH", "OVN"],
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
            if side in ("BUY", "B", "G", "NB"):
                buy += size
            elif side in ("SELL", "S", "L", "NS"):
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
    cursor = int(end.timestamp() * 1000) - 1
    valid = {}
    covered = False
    for _ in range(4):
        response = broker.session.data_client.market_data.get_batch_history_bar(
            [symbol], US_STOCK_CATEGORY, "M5", count=1200, real_time_required=True,
            trading_sessions=["PRE", "RTH", "ATH", "OVN"], end_time=cursor,
        )
        if response.status_code != 200:
            if response.status_code == 429:
                raise RuntimeError("HTTP Status: 429; historical data rate limited")
            return None
        payload = response.json()
        if isinstance(payload, dict) and (payload.get("partial") is True or payload.get("complete") is False):
            return None
        series = payload.get("result", []) if isinstance(payload, dict) else []
        if isinstance(series, list) and any(isinstance(item, dict) and item.get("symbol") and item["symbol"].upper() != symbol.upper() for item in series):
            return None
        bars = bars_from_response(payload)
        if not bars:
            covered = True
            break
        stamps = []
        for bar in bars:
            stamp = market_time(bar.time)
            if stamp is None or int(stamp.timestamp() * 1000) > cursor:
                return None
            if not Decimal(str(bar.volume)).is_finite() or bar.volume < 0:
                return None
            stamps.append(stamp)
            if start <= stamp < end and bar.volume > 0:
                if any(not Decimal(str(value)).is_finite() or value <= 0 for value in (bar.high, bar.low)) or bar.low > bar.high:
                    return None
                valid[stamp] = bar
        earliest = min(stamps)
        if earliest <= start:
            covered = True
            break
        next_cursor = int(earliest.timestamp() * 1000) - 1
        if next_cursor >= cursor:
            return None
        cursor = next_cursor
    if not covered or not valid:
        return None
    return Decimal(str(max(bar.high for bar in valid.values()))), Decimal(str(min(bar.low for bar in valid.values())))