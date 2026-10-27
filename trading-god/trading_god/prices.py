"""Latest-price lookup: moomoo first, Yahoo Finance when moomoo has no quote.

moomoo's API only serves US quotes to accounts with a US quote entitlement,
so the risk checks fall back to Yahoo (already installed with Vibe-Trading)
rather than refusing every order.
"""

from __future__ import annotations

import logging
import math
from typing import Callable

logger = logging.getLogger(__name__)

PriceLookup = Callable[[str], "float | None"]


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
