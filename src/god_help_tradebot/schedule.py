from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class TradingSession:
    session_date: date
    market_open: datetime
    market_close: datetime

    @property
    def exit_time(self) -> datetime:
        return self.market_close - timedelta(minutes=3)


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


def _holidays(year: int) -> set[date]:
    new_year = _observed(date(year, 1, 1))
    independence = _observed(date(year, 7, 4))
    christmas = _observed(date(year, 12, 25))
    thanksgiving = date(year, 11, 1)
    thanksgiving += timedelta(days=(3 - thanksgiving.weekday()) % 7 + 21)
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
    return {
        new_year,
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


def _early_close(day: date) -> bool:
    if day.weekday() == 4 and 22 <= day.day <= 28 and day.month == 11:
        return True
    if day.month == 12 and day.day == 24 and day.weekday() < 5:
        return True
    if day.month == 7 and day.day == 3 and day.weekday() < 5:
        return True
    return False


def get_trading_session(session_date: date) -> TradingSession | None:
    if session_date.weekday() >= 5 or session_date in _holidays(session_date.year):
        return None
    close = time(13 if _early_close(session_date) else 16)
    return TradingSession(
        session_date,
        datetime.combine(session_date, time(9, 30), EASTERN),
        datetime.combine(session_date, close, EASTERN),
    )