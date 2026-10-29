"""Shared fakes: a paper account in memory, a fixed clock and a desk built on them."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from trading_god.broker import Account, BrokerError, Position
from trading_god.config import Settings
from trading_god.desk import Desk
from trading_god.risk import RiskLimits
from trading_god.store import ProposalStore

# 10:00 New York time on a Tuesday.
START = datetime(2026, 10, 27, 14, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class FakeBroker:
    """Just enough of MoomooPaperBroker for the desk and the page."""

    def __init__(self, equity: float = 1_000_000.0, cash: float = 1_000_000.0) -> None:
        self.equity = equity
        self.cash = cash
        self.positions: list[Position] = []
        self.open_buys: dict[str, float] | None = {}
        self.placed: list[tuple[Any, str]] = []
        self.account_error: Exception | None = None
        self.place_error: Exception | None = None

    def hold(self, symbol: str, quantity: float, market_value: float, sellable: float | None = None) -> None:
        self.positions.append(
            Position(symbol, symbol, quantity, quantity if sellable is None else sellable, market_value, None, None)
        )

    def account(self) -> Account:
        if self.account_error:
            raise self.account_error
        return Account(
            acc_id=1,
            currency="USD",
            equity=self.equity,
            cash=self.cash,
            buying_power=self.cash,
            market_value=sum(p.market_value for p in self.positions),
            positions=tuple(self.positions),
            open_buys=None if self.open_buys is None else dict(self.open_buys),
        )

    def place_order(self, order: Any, remark: str) -> dict[str, Any]:
        if self.place_error:
            raise self.place_error
        self.placed.append((order, remark))
        order_id = f"ORD{len(self.placed)}"
        return {"order_id": order_id, "acc_id": 1, "moomoo": {"order_id": order_id}}

    def recent_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        return [{"code": o.symbol, "trd_side": o.side.upper(), "qty": o.quantity} for o, _ in self.placed][:limit]

    def describe_accounts(self) -> dict[str, Any]:
        return {"gateway_open": True, "paper_account_in_use": 1}


class Prices:
    def __init__(self, **prices: float) -> None:
        self.prices = {f"US.{k}": v for k, v in prices.items()}

    def __call__(self, symbol: str) -> float | None:
        return self.prices.get(symbol)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep every test away from the real ~/.trading-god and ~/.vibe-trading."""
    monkeypatch.setenv("TRADING_GOD_HOME", str(tmp_path / "tg-home"))
    monkeypatch.setenv("VIBE_TRADING_HOME", str(tmp_path / "vibe-home"))
    return tmp_path


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


@pytest.fixture
def prices() -> Prices:
    return Prices(AAPL=200.0, MSFT=400.0, NVDA=150.0, PENNY=2.0)


@pytest.fixture
def notes() -> list[str]:
    return []


@pytest.fixture
def make_desk(tmp_path, broker, prices, clock, notes):
    def build(**risk: Any) -> Desk:
        settings = replace(Settings(), risk=RiskLimits(**risk)) if risk else Settings()
        return Desk(settings, ProposalStore(tmp_path / "desk.db"), broker, prices, notifier=notes.append, clock=clock)

    return build


@pytest.fixture
def desk(make_desk) -> Desk:
    return make_desk()


def buy(
    desk: Desk,
    symbol: str = "AAPL",
    quantity: int = 100,
    limit: float = 200.0,
    stop: float = 185.0,
    playbook: str = "minervini-vcp + lynch-growth",
    **extra: Any,
):
    return desk.propose(
        symbol=symbol,
        side="buy",
        quantity=quantity,
        thesis="Breakout from a seven-week base on rising volume; wrong below the base low.",
        playbook=playbook,
        limit_price=limit,
        stop_loss=stop,
        **extra,
    )


__all__ = ["BrokerError", "Clock", "FakeBroker", "Prices", "START", "buy"]
