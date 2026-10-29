import sys
import types
from datetime import date, datetime, timezone

import pandas as pd

from trading_god.prices import Bar, yahoo_daily_bars, yahoo_next_earnings, yahoo_ticker

TODAY = date(2026, 10, 27)


def fake_yfinance(monkeypatch, *, calendar=None, history=None):
    asked = []

    class Ticker:
        def __init__(self, symbol):
            asked.append(symbol)
            self.calendar = calendar

        def history(self, **kwargs):
            asked.append(kwargs)
            return history

        def get_earnings_dates(self, limit=8):
            if isinstance(history, Exception):
                raise history
            return history

    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(Ticker=Ticker))
    return asked


def test_yahoo_ticker_uses_dashes():
    assert yahoo_ticker("US.BRK.B") == "BRK-B"


def test_next_earnings_comes_from_the_calendar(monkeypatch):
    asked = fake_yfinance(monkeypatch, calendar={"Earnings Date": [date(2026, 10, 29), date(2026, 10, 30)]})
    assert yahoo_next_earnings("US.MA", TODAY) == date(2026, 10, 29)
    assert asked == ["MA"]


def test_past_calendar_dates_fall_back_to_the_earnings_history(monkeypatch):
    index = pd.DatetimeIndex(
        [datetime(2027, 1, 29, 13, tzinfo=timezone.utc), datetime(2026, 7, 31, 13, tzinfo=timezone.utc)]
    )
    fake_yfinance(
        monkeypatch,
        calendar={"Earnings Date": [date(2026, 7, 31)]},
        history=pd.DataFrame({"EPS Estimate": [1.0, 1.0]}, index=index),
    )
    assert yahoo_next_earnings("US.XOM", TODAY) == date(2027, 1, 29)


def test_no_date_anywhere_is_unknown(monkeypatch):
    fake_yfinance(monkeypatch, calendar={}, history=RuntimeError("blocked"))
    assert yahoo_next_earnings("US.XOM", TODAY) is None


def test_daily_bars_skip_broken_rows(monkeypatch):
    index = pd.DatetimeIndex(
        [datetime(2026, 10, 26, 4, tzinfo=timezone.utc), datetime(2026, 10, 27, 4, tzinfo=timezone.utc)]
    )
    frame = pd.DataFrame({"High": [101.0, float("nan")], "Low": [99.0, 98.0], "Close": [100.0, 99.0]}, index=index)
    asked = fake_yfinance(monkeypatch, history=frame)
    assert yahoo_daily_bars("US.BRK.B", TODAY) == [Bar(date(2026, 10, 26), 101.0, 99.0, 100.0)]
    assert asked[0] == "BRK-B" and asked[1]["start"] == "2026-09-15" and asked[1]["auto_adjust"] is False
