from datetime import date, datetime, time

from god_help_tradebot.schedule import EASTERN, apply_verified_closure_update, get_trading_session


def test_weekday_session_has_one_minute_exit_buffer() -> None:
    session = get_trading_session(date(2026, 9, 28))

    assert session is not None
    assert session.market_open == datetime(2026, 9, 28, 9, 30, tzinfo=EASTERN)
    assert session.exit_time.time() == time(15, 59)


def test_holiday_is_not_tradable() -> None:
    assert get_trading_session(date(2026, 12, 25)) is None


def test_observed_new_year_edges() -> None:
    assert get_trading_session(date(2027, 12, 31)) is not None
    assert get_trading_session(date(2028, 1, 1)) is None


def test_verified_override_persists_and_controls_session(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    applied = apply_verified_closure_update(
        closed_dates={date(2026, 12, 28)},
        early_close_dates={date(2026, 12, 29): time(13)},
    )
    assert applied == 2
    assert get_trading_session(date(2026, 12, 28)) is None
    session = get_trading_session(date(2026, 12, 29))
    assert session is not None
    assert session.market_close.time() == time(13)


def test_friday_observed_independence_and_christmas_are_closed() -> None:
    # NYSE: Friday July 3, 2026 and Friday Dec 24, 2027 are observed holidays.
    assert get_trading_session(date(2026, 7, 3)) is None
    assert get_trading_session(date(2027, 12, 24)) is None
    assert get_trading_session(date(2026, 7, 2)).market_close.time() == time(16)
    assert get_trading_session(date(2027, 12, 23)).market_close.time() == time(16)


def test_weekday_eves_of_independence_and_christmas_close_early() -> None:
    assert get_trading_session(date(2028, 7, 3)).market_close.time() == time(13)
    assert get_trading_session(date(2026, 12, 24)).market_close.time() == time(13)


def test_black_friday_follows_thanksgiving_every_year() -> None:
    # Thanksgiving 2030 is Nov 28, so the early close is Nov 29, not Nov 22.
    assert get_trading_session(date(2030, 11, 28)) is None
    assert get_trading_session(date(2030, 11, 29)).market_close.time() == time(13)
    assert get_trading_session(date(2030, 11, 22)).market_close.time() == time(16)


def test_known_early_close_has_1259_exit() -> None:
    session = get_trading_session(date(2026, 11, 27))

    assert session is not None
    assert session.market_close.time() == time(13)
    assert session.exit_time.time() == time(12, 59)