from datetime import date, datetime, time

from god_help_tradebot.schedule import EASTERN, get_trading_session


def test_weekday_session_has_three_minute_exit_buffer() -> None:
    session = get_trading_session(date(2026, 9, 28))

    assert session is not None
    assert session.market_open == datetime(2026, 9, 28, 9, 30, tzinfo=EASTERN)
    assert session.exit_time.time() == time(15, 57)


def test_holiday_is_not_tradable() -> None:
    assert get_trading_session(date(2026, 12, 25)) is None


def test_known_early_close_has_1257_exit() -> None:
    session = get_trading_session(date(2026, 11, 27))

    assert session is not None
    assert session.market_close.time() == time(13)
    assert session.exit_time.time() == time(12, 57)