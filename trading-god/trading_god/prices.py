"""Latest-price lookup: moomoo first, Yahoo Finance when moomoo has no quote.

moomoo's API only serves US quotes to accounts with a US quote entitlement,
so the risk checks fall back to Yahoo (already installed with Vibe-Trading)
rather than refusing every order.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

PriceLookup = Callable[[str], "float | None"]
#: ``(symbol, today) -> next earnings date on or after today``, or None if unknown.
EarningsLookup = Callable[[str, date], "date | None"]


def yahoo_ticker(symbol: str) -> str:
    """``US.BRK.B`` -> ``BRK-B``, the form Yahoo uses."""
    code = symbol[len("US.") :] if symbol.startswith("US.") else symbol
    return code.replace(".", "-")


def yahoo_last_price(symbol: str) -> float | None:
    try:
        import yfinance as yf

        info = yf.Ticker(yahoo_ticker(symbol)).fast_info
        price = info["last_price"]
    except Exception as exc:  # noqa: BLE001 - any failure means "no quote"
        logger.info("Yahoo quote for %s failed: %s", symbol, exc)
        return None
    try:
        price = float(price)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def first_price(*lookups: PriceLookup) -> PriceLookup:
    """Combine lookups: the first one that returns a price wins."""

    def lookup(symbol: str) -> float | None:
        for source in lookups:
            price = source(symbol)
            if price is not None:
                return price
        return None

    return lookup


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    to_pydatetime = getattr(value, "to_pydatetime", None)  # pandas Timestamp
    if callable(to_pydatetime):
        return to_pydatetime().date()
    return None


def next_on_or_after(values: Iterable[Any], today: date) -> date | None:
    days = [d for d in (_as_date(v) for v in values) if d is not None and d >= today]
    return min(days) if days else None


def yahoo_next_earnings(symbol: str, today: date) -> date | None:
    """The next earnings date Yahoo lists, from the calendar or the earnings history."""
    try:
        import yfinance as yf

        ticker = yf.Ticker(yahoo_ticker(symbol))
    except Exception as exc:  # noqa: BLE001
        logger.info("Yahoo earnings lookup for %s failed: %s", symbol, exc)
        return None
    try:
        calendar = ticker.calendar
        dates = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
        if dates is not None:
            found = next_on_or_after(dates if isinstance(dates, (list, tuple)) else [dates], today)
            if found is not None:
                return found
    except Exception as exc:  # noqa: BLE001
        logger.info("Yahoo calendar for %s failed: %s", symbol, exc)
    try:
        frame = ticker.get_earnings_dates(limit=8)
        if frame is not None and len(frame.index):
            return next_on_or_after(list(frame.index), today)
    except Exception as exc:  # noqa: BLE001
        logger.info("Yahoo earnings dates for %s failed: %s", symbol, exc)
    return None
