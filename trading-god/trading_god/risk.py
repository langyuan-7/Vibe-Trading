"""Pre-trade risk checks for proposed orders.

Every check here is a pure function of the order, the latest price, the
account and the limits, so the same rules run twice: when the agent proposes
an order, and again when a person approves it against fresh account state.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping

#: A US stock or ETF in moomoo's code format, e.g. ``US.AAPL`` or ``US.BRK.B``.
#: Digits are rejected on purpose: option contracts look like
#: ``US.AAPL261218C200000``.
US_SYMBOL_RE = re.compile(r"^US\.[A-Z]{1,5}(?:\.[A-Z]{1,2})?$")

SIDES = ("buy", "sell")
ORDER_TYPES = ("limit", "market")


def normalize_symbol(raw: str) -> str:
    """Return ``raw`` in moomoo's ``US.TICKER`` form.

    Accepts ``aapl``, ``AAPL``, ``US.AAPL``, ``AAPL.US`` and share classes
    written as ``BRK-B``, ``BRK/B`` or ``BRK.B``. The result is not validated;
    :func:`evaluate` rejects anything that is not a plain US stock code.
    """
    code = str(raw or "").strip().upper()
    if code.endswith(".US"):
        code = code[: -len(".US")]
    if code.startswith("US."):
        code = code[len("US.") :]
    code = code.replace("-", ".").replace("/", ".")
    return f"US.{code}"


@dataclass(frozen=True)
class RiskLimits:
    """Hard limits every order must pass, as set in ``desk.toml``.

    Percentages are of account equity (total assets) at the time of the check.
    """

    max_order_pct: float = 5.0
    max_position_pct: float = 10.0
    max_invested_pct: float = 90.0
    max_buys_per_day: int = 5
    max_pending: int = 10
    min_price: float = 5.0
    max_limit_deviation_pct: float = 15.0
    allow_market_orders: bool = True
    market_order_buffer_pct: float = 1.0
    blocked_symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("max_order_pct", "max_position_pct", "max_invested_pct"):
            value = getattr(self, name)
            if not 0 < value <= 100:
                raise ValueError(f"risk.{name} must be above 0 and at most 100, got {value}")
        if self.max_buys_per_day < 0 or self.max_pending < 1:
            raise ValueError("risk.max_buys_per_day must be >= 0 and risk.max_pending >= 1")
        if self.min_price < 0 or self.max_limit_deviation_pct <= 0 or self.market_order_buffer_pct < 0:
            raise ValueError("risk.min_price, max_limit_deviation_pct and market_order_buffer_pct must be positive")
        object.__setattr__(self, "blocked_symbols", tuple(normalize_symbol(s) for s in self.blocked_symbols))


@dataclass(frozen=True)
class OrderRequest:
    """One order as the agent proposed it."""

    symbol: str
    side: str
    quantity: int
    order_type: str = "limit"
    limit_price: float | None = None


@dataclass(frozen=True)
class Holding:
    """One open position, keyed by moomoo code.

    ``sellable`` excludes shares already in an open sell order, so two exits
    cannot add up to more than is held.
    """

    symbol: str
    sellable: float
    market_value: float


@dataclass(frozen=True)
class AccountState:
    """What the checks need to know about the account right now."""

    equity: float
    cash: float
    holdings: Mapping[str, Holding] = field(default_factory=dict)
    #: Unfilled value of open buy orders by symbol, or ``None`` when unknown.
    open_buys: Mapping[str, float] | None = field(default_factory=dict)
    buys_today: int = 0
    pending: int = 0

    @property
    def invested(self) -> float:
        return sum(max(h.market_value, 0.0) for h in self.holdings.values())

    @property
    def committed(self) -> float:
        """Cash already promised to open buy orders."""
        return sum(max(value, 0.0) for value in (self.open_buys or {}).values())


@dataclass(frozen=True)
class Check:
    """The outcome of one rule."""

    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class RiskReport:
    """All rule outcomes for one order, plus the price and size they used."""

    checks: tuple[Check, ...]
    price: float | None
    notional: float | None

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def failures(self) -> list[Check]:
        return [check for check in self.checks if not check.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "price": self.price,
            "notional": self.notional,
            "checks": [asdict(check) for check in self.checks],
        }


def check_exit_plan(side: str, entry: float | None, stop_loss: float | None, take_profit: float | None) -> list[Check]:
    """Every buy must say where it is wrong (a stop below entry) before it is sized.

    Sells are exits, so they carry no plan requirement.
    """
    if side != "buy":
        return []
    checks: list[Check] = []
    if stop_loss is None or not math.isfinite(stop_loss) or stop_loss <= 0:
        checks.append(
            Check("stop_loss", False, "every buy needs a stop_loss price: the level that proves the idea wrong")
        )
    elif entry is not None and stop_loss >= entry:
        checks.append(
            Check("stop_loss", False, f"stop_loss {_usd(stop_loss)} must be below the entry price {_usd(entry)}")
        )
    else:
        detail = f"stop at {_usd(stop_loss)}"
        if entry:
            detail += f", {100.0 * (entry - stop_loss) / entry:.1f}% below entry"
        checks.append(Check("stop_loss", True, detail))
    if take_profit is not None:
        target_ok = math.isfinite(take_profit) and (entry is None or take_profit > entry)
        checks.append(
            Check(
                "take_profit",
                target_ok,
                f"target at {_usd(take_profit)}"
                if target_ok
                else f"take_profit {_usd(take_profit)} must be above the entry price",
            )
        )
    return checks


def _pct(part: float, whole: float) -> float:
    return 100.0 * part / whole if whole > 0 else math.inf


def _usd(value: float) -> str:
    return f"${value:,.2f}"


def evaluate(
    order: OrderRequest,
    last_price: float | None,
    account: AccountState,
    limits: RiskLimits,
    *,
    include_pending: bool = True,
) -> RiskReport:
    """Run every rule against ``order`` and report each outcome.

    Args:
        order: The order, with ``symbol`` already normalized.
        last_price: Latest trade price, or ``None`` when no quote was found.
        account: Current equity, cash, holdings and today's order count.
        limits: The configured hard limits.
        include_pending: Count the queue of waiting proposals. True when the
            agent proposes; False at approval, where the proposal being
            approved is itself one of the pending ones.

    Returns:
        A :class:`RiskReport`. The order may go ahead only if ``passed``.
    """
    checks: list[Check] = []

    def add(name: str, passed: bool, detail: str) -> None:
        checks.append(Check(name, bool(passed), detail))

    symbol_ok = bool(US_SYMBOL_RE.match(order.symbol))
    add(
        "symbol",
        symbol_ok,
        f"{order.symbol} is a US stock or ETF code"
        if symbol_ok
        else f"{order.symbol} is not allowed: only US stock and ETF codes like US.AAPL (no options, crypto or other markets)",
    )
    if order.symbol in limits.blocked_symbols:
        add("not_blocked", False, f"{order.symbol} is on the blocked list in desk.toml")

    side_ok = order.side in SIDES
    add("side", side_ok, f"side is {order.side}" if side_ok else f"side must be buy or sell, got {order.side!r}")

    quantity_ok = isinstance(order.quantity, int) and not isinstance(order.quantity, bool) and order.quantity > 0
    add(
        "quantity",
        quantity_ok,
        f"{order.quantity} shares"
        if quantity_ok
        else f"quantity must be a whole number of shares above 0, got {order.quantity!r}",
    )

    if order.order_type == "limit":
        limit_ok = order.limit_price is not None and math.isfinite(order.limit_price) and order.limit_price > 0
        add(
            "order_type",
            limit_ok,
            f"limit order at {_usd(order.limit_price)}" if limit_ok else "a limit order needs a limit_price above 0",
        )
    elif order.order_type == "market":
        add(
            "order_type",
            limits.allow_market_orders,
            "market order"
            if limits.allow_market_orders
            else "market orders are turned off in desk.toml; use a limit order",
        )
    else:
        add("order_type", False, f"order_type must be limit or market, got {order.order_type!r}")

    price_ok = last_price is not None and math.isfinite(last_price) and last_price > 0
    add(
        "quote",
        price_ok,
        f"last price {_usd(last_price)}" if price_ok else f"no current price found for {order.symbol}",
    )
    if not (price_ok and side_ok and quantity_ok):
        return RiskReport(tuple(checks), last_price if price_ok else None, None)

    if order.side == "buy":
        add(
            "min_price",
            last_price >= limits.min_price,
            f"{_usd(last_price)} is at or above the {_usd(limits.min_price)} minimum"
            if last_price >= limits.min_price
            else f"{_usd(last_price)} is below the {_usd(limits.min_price)} minimum share price",
        )

    if order.order_type == "limit" and order.limit_price:
        deviation = abs(_pct(order.limit_price, last_price) - 100.0)
        add(
            "limit_near_market",
            deviation <= limits.max_limit_deviation_pct,
            f"limit is {deviation:.1f}% from the last price"
            if deviation <= limits.max_limit_deviation_pct
            else f"limit {_usd(order.limit_price)} is {deviation:.1f}% from the last price {_usd(last_price)} "
            f"(max {limits.max_limit_deviation_pct:g}%)",
        )

    # Size an order by the most it can cost: the limit for a buy limit, the
    # last price plus a cushion for a buy at market.
    if order.order_type == "limit" and order.limit_price:
        unit = order.limit_price
    elif order.side == "buy":
        unit = last_price * (1 + limits.market_order_buffer_pct / 100.0)
    else:
        unit = last_price
    notional = round(unit * order.quantity, 2)

    holding = account.holdings.get(order.symbol)
    held_value = holding.market_value if holding else 0.0
    sellable = holding.sellable if holding else 0.0

    # Selling what is held only ever reduces risk, so a sell is checked for
    # shorting alone: a stop-loss exit must never be blocked by a size cap or
    # by the day's order count.
    if order.side == "buy":
        if account.equity <= 0:
            add("equity", False, "account equity is not above 0, so no order can be sized")
            return RiskReport(tuple(checks), last_price, notional)
        if account.open_buys is None:
            add("open_orders", False, "moomoo did not return today's open orders, so this buy cannot be sized safely")
            return RiskReport(tuple(checks), last_price, notional)
        committed = account.committed
        order_pct = _pct(notional, account.equity)
        add(
            "order_size",
            order_pct <= limits.max_order_pct,
            f"{_usd(notional)} is {order_pct:.2f}% of equity (max {limits.max_order_pct:g}%)",
        )
        free_cash = account.cash - committed
        add(
            "cash",
            notional <= free_cash,
            f"{_usd(notional)} needed, {_usd(free_cash)} cash free"
            + (f" after {_usd(committed)} in open buy orders" if committed else "")
            + ("" if notional <= free_cash else " (no margin)"),
        )
        position_pct = _pct(held_value + account.open_buys.get(order.symbol, 0.0) + notional, account.equity)
        add(
            "position_size",
            position_pct <= limits.max_position_pct,
            f"{order.symbol} would be {position_pct:.2f}% of equity, counting open orders "
            f"(max {limits.max_position_pct:g}%)",
        )
        invested_pct = _pct(account.invested + committed + notional, account.equity)
        add(
            "invested",
            invested_pct <= limits.max_invested_pct,
            f"stocks would be {invested_pct:.2f}% of equity (max {limits.max_invested_pct:g}%)",
        )
        add(
            "daily_orders",
            account.buys_today < limits.max_buys_per_day,
            f"{account.buys_today} of {limits.max_buys_per_day} buy orders placed today",
        )
    else:
        add(
            "no_short",
            order.quantity <= sellable,
            f"selling {order.quantity} of {sellable:g} sellable shares"
            if order.quantity <= sellable
            else f"cannot sell {order.quantity} shares of {order.symbol}: only {sellable:g} sellable "
            "(no short selling; shares in an open sell order are not sellable)",
        )

    if include_pending:
        add(
            "pending_queue",
            account.pending < limits.max_pending,
            f"{account.pending} of {limits.max_pending} proposals already waiting for approval",
        )

    return RiskReport(tuple(checks), last_price, notional)
