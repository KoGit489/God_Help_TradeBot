from dataclasses import dataclass
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


def equity_order_session(now: datetime) -> str:
    now = now.astimezone(EASTERN)
    today = get_trading_session(now.date())
    if today is not None:
        if today.market_open <= now < today.market_close:
            return "CORE"
        if time(4) <= now.time() < extended_close(today):
            return "ALL"
        if now.time() < time(4):
            return "NIGHT"
    if now.time() >= time(20) and get_trading_session(now.date() + timedelta(days=1)) is not None:
        return "NIGHT"
    return "CLOSED"


def extended_close(session: "TradingSession") -> time:
    """Late trading ends at 5 p.m. on early-close days, otherwise 8 p.m."""
    return time(17) if session.market_close.time() < time(16) else time(20)


def last_trading_minute(day: date) -> datetime | None:
    """Final tradable minute of an Eastern calendar day, derived from the exchange calendar."""
    if get_trading_session(day + timedelta(days=1)) is not None:
        return datetime.combine(day, time(23, 59), EASTERN)
    session = get_trading_session(day)
    if session is None:
        return None
    return datetime.combine(day, extended_close(session), EASTERN) - timedelta(minutes=1)


@dataclass(frozen=True)
class TradingSession:
    session_date: date
    market_open: datetime
    market_close: datetime

    @property
    def exit_time(self) -> datetime:
        return self.market_close - timedelta(minutes=1)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _easter_sunday(year: int) -> date:
    century = year // 100
    remainder = year % 19
    skipped_leap_years = (century - century // 4 - (8 * century + 13) // 25) % 30
    moon = (19 * remainder + century - century // 4 - skipped_leap_years + 15) % 30
    weekday = (year + year // 4 + moon + 2 - century + century // 4) % 7
    month = 3 + (moon + weekday + 22) // 31
    day = (moon + weekday + 22) % 31 + 1
    return date(year, month, day)


def _new_year_holiday(year: int) -> date | None:
    day = date(year, 1, 1)
    if day.weekday() == 5:
        # 2022 was observed on December 31, 2021; 2028 had no observed date.
        if year == 2022:
            return date(2021, 12, 31)
        return None
    if day.weekday() == 6:
        return date(year, 1, 2)
    return day


def _holidays(year: int) -> set[date]:
    new_year = _new_year_holiday(year)
    independence = _observed(date(year, 7, 4))
    christmas = _observed(date(year, 12, 25))
    thanksgiving = _thanksgiving(year)
    memorial = date(year, 5, 31)
    memorial -= timedelta(days=(memorial.weekday() - 0) % 7)
    labor = date(year, 9, 1)
    labor += timedelta(days=(0 - labor.weekday()) % 7)
    mlk = date(year, 1, 1)
    mlk += timedelta(days=(0 - mlk.weekday()) % 7 + 14)
    presidents = date(year, 2, 1)
    presidents += timedelta(days=(0 - presidents.weekday()) % 7 + 14)
    juneteenth = _observed(date(year, 6, 19))
    good_friday = _easter_sunday(year) - timedelta(days=2)
    holidays = {
        mlk,
        presidents,
        memorial,
        juneteenth,
        independence,
        labor,
        thanksgiving,
        christmas,
        good_friday,
    }
    if new_year is not None:
        holidays.add(new_year)
    holidays.update(_load_override_calendar().get("closed_dates", set()))
    holidays.difference_update(_known_early_close_dates(year))
    return {day for day in holidays if day.year == year}


def _thanksgiving(year: int) -> date:
    first = date(year, 11, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 21)


def _known_early_close_dates(year: int) -> set[date]:
    """1 p.m. closes: the day after Thanksgiving, plus July 3 and December 24
    when they fall Monday-Thursday. On a Friday those dates are the observed
    Independence Day / Christmas holiday instead, so the market is closed."""
    dates = {_thanksgiving(year) + timedelta(days=1)}
    for month, day in ((7, 3), (12, 24)):
        candidate = date(year, month, day)
        if candidate.weekday() < 4:
            dates.add(candidate)
    return dates


def _early_close(day: date) -> bool:
    if day.isoformat() in _load_override_calendar().get("early_close_dates", {}):
        return True
    return day in _known_early_close_dates(day.year)


def _exchange_calendar_path() -> Path:
    return Path.cwd() / "exchange_calendar.json"


def _load_override_calendar() -> dict[str, Any]:
    path = _exchange_calendar_path()
    if not path.exists():
        return {"closed_dates": set(), "early_close_dates": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        closed = {date.fromisoformat(value) for value in data.get("closed_dates", []) if isinstance(value, str)}
        early = {value: str(close).replace(".", ":") for value, close in data.get("early_close_dates", {}).items() if isinstance(value, str) and isinstance(close, str)}
        return {"closed_dates": closed, "early_close_dates": early}
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return {"closed_dates": set(), "early_close_dates": {}}


def apply_verified_closure_update(closed_dates: set[date] | None = None, early_close_dates: dict[date, time] | None = None) -> int:
    path = _exchange_calendar_path()
    current = _load_override_calendar()
    closed = set(current["closed_dates"])
    early = dict(current["early_close_dates"])
    if closed_dates:
        closed.update(closed_dates)
    if early_close_dates:
        for day, close_time in early_close_dates.items():
            early[day.isoformat()] = close_time.isoformat(timespec="minutes")
    payload = {
        "closed_dates": sorted(day.isoformat() for day in closed),
        "early_close_dates": dict(sorted(early.items())),
    }
    new_entries = (
        len(set(closed_dates or set()) - set(current["closed_dates"]))
        + len(set(early_close_dates or {}) - set(current["early_close_dates"]))
    )
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return new_entries


def get_trading_session(session_date: date) -> TradingSession | None:
    if session_date.weekday() >= 5 or session_date in _holidays(session_date.year):
        return None
    if session_date.isoformat() in _load_override_calendar().get("early_close_dates", {}):
        close_text = _load_override_calendar()["early_close_dates"][session_date.isoformat()]
        try:
            close = time.fromisoformat(close_text)
        except ValueError:
            close = time(13)
    else:
        close = time(13 if _early_close(session_date) else 16)
    return TradingSession(
        session_date,
        datetime.combine(session_date, time(9, 30), EASTERN),
        datetime.combine(session_date, close, EASTERN),
    )