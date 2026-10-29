"""The approval desk: the only path from the agent to the broker.

The agent can read the portfolio, propose an order and read back decisions.
Only a person, through the desk page or the ``tg-desk`` command, can approve a
proposal, and approval re-runs every risk check against fresh account state
before the order is sent to the paper account.

One sell needs no approval: when a position's price falls to the stop of the buy
a person approved, the desk sells it at market during market hours
(``[exits] auto_stop``), because a stop that waits for someone is no stop.
Stops only ever move up: the desk trails them behind the highest close, and the
agent may raise one to a level its playbook names.
"""

from __future__ import annotations

import logging
import math
import threading
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trading_god import store as st
from trading_god.broker import Account, BrokerError, MoomooPaperBroker
from trading_god.config import Settings
from trading_god.prices import BarsLookup, EarningsLookup, PriceLookup
from trading_god.risk import (
    AccountState,
    Check,
    Holding,
    OrderRequest,
    RiskReport,
    check_earnings,
    check_exit_plan,
    evaluate,
    normalize_symbol,
    trailing_stop,
)

logger = logging.getLogger(__name__)

Notifier = Callable[[str], None]

_MAX_THESIS = 4000
_MIN_THESIS = 30

#: The playbook of a sell the desk placed itself because a stop was hit.
AUTO_STOP = "auto-stop"
#: The agent may not raise a stop closer than this to the last price.
_MIN_STOP_ROOM_PCT = 2.0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """One timestamp format everywhere, so stored values compare as strings."""
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def us_trading_day_start(moment: datetime) -> datetime:
    """Midnight New York time on ``moment``'s US calendar day, in UTC."""
    try:
        new_york = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:  # no tz database: count the UTC day instead
        return moment.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    local = moment.astimezone(new_york)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def us_date(moment: datetime) -> date:
    """``moment``'s calendar day in New York."""
    try:
        return moment.astimezone(ZoneInfo("America/New_York")).date()
    except ZoneInfoNotFoundError:
        return moment.astimezone(timezone.utc).date()


def us_session_open(moment: datetime) -> bool:
    """Regular US hours, 9:30 to 16:00 New York on a weekday. Exchange holidays are not known."""
    try:
        local = moment.astimezone(ZoneInfo("America/New_York"))
    except ZoneInfoNotFoundError:  # cannot tell the session, so never sell on a guess
        return False
    return local.weekday() < 5 and time(9, 30) <= local.time() < time(16, 0)


def proposal_expiry(now: datetime, ttl_hours: float) -> datetime:
    """``ttl_hours`` from now, but never before the next regular US session closes.

    A scan run in the evening would otherwise expire overnight, before anyone could
    approve it during market hours. Exchange holidays are not known.
    """
    expires = now + timedelta(hours=ttl_hours)
    try:
        new_york = ZoneInfo("America/New_York")
    except ZoneInfoNotFoundError:
        return expires
    local = now.astimezone(new_york)
    day = local.date()
    if day.weekday() >= 5 or local.time() >= time(16, 0):
        day += timedelta(days=1)
        while day.weekday() >= 5:
            day += timedelta(days=1)
    close = datetime.combine(day, time(16, 0), tzinfo=new_york).astimezone(timezone.utc)
    return max(expires, close)


def _optional_float(value: Any, name: str) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


def _whole_shares(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("quantity must be a whole number of shares") from exc
    if not number.is_integer():
        raise ValueError("quantity must be a whole number of shares")
    return int(number)


def order_fields(proposal: st.Proposal) -> dict[str, Any]:
    """The order as numbers, so the agent can quote them exactly.

    Vibe-Trading's grounding gate only accepts a figure that a tool returned as a
    number; one inside the ``summary`` text does not count.
    """
    return {
        "symbol": proposal.symbol,
        "side": proposal.side,
        "quantity": proposal.quantity,
        "order_type": proposal.order_type,
        "limit_price": proposal.limit_price,
        "stop_loss": proposal.stop_loss,
        "take_profit": proposal.take_profit,
        "notional": round(proposal.notional, 2) if proposal.notional else proposal.notional,
    }


def describe(proposal: st.Proposal) -> str:
    """One line a person can read at a glance."""
    price = f"limit ${proposal.limit_price:,.2f}" if proposal.order_type == "limit" else "at market"
    size = f" (~${proposal.notional:,.0f})" if proposal.notional else ""
    return f"#{proposal.id} {proposal.side.upper()} {proposal.quantity} {proposal.symbol} {price}{size}"


class Desk:
    """Proposals in, approved paper orders out."""

    def __init__(
        self,
        settings: Settings,
        store: st.ProposalStore,
        broker: MoomooPaperBroker,
        prices: PriceLookup,
        *,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = utc_now,
        earnings: EarningsLookup | None = None,
        bars: BarsLookup | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.broker = broker
        self.prices = prices
        self.notifier = notifier
        self.clock = clock
        self.earnings = earnings
        self._earnings_seen: dict[tuple[str, date], date | None] = {}
        self.bars = bars
        self._trailed: set[tuple[int, date]] = set()
        self._trail_tried: dict[int, datetime] = {}

    # -- shared helpers ---------------------------------------------------

    def _state(
        self, account: Account, now: datetime, *, include_pending: bool, exclude_id: int | None = None
    ) -> AccountState:
        holdings = {p.symbol: Holding(p.symbol, p.sellable, p.market_value) for p in account.positions}
        return AccountState(
            equity=account.equity,
            cash=account.cash,
            holdings=holdings,
            open_buys=account.open_buys,
            buys_today=self.store.count_placed_buys_since(iso(us_trading_day_start(now)), exclude_id=exclude_id),
            pending=self.store.count(st.PENDING) if include_pending else 0,
        )

    def _report(
        self,
        order: OrderRequest,
        account: Account,
        now: datetime,
        *,
        stop_loss: float | None,
        take_profit: float | None,
        include_pending: bool,
        exclude_id: int | None = None,
    ) -> RiskReport:
        price = self.prices(order.symbol)
        report = evaluate(
            order,
            price,
            self._state(account, now, include_pending=include_pending, exclude_id=exclude_id),
            self.settings.risk,
            include_pending=include_pending,
        )
        entry = order.limit_price if order.order_type == "limit" and order.limit_price else price
        plan = check_exit_plan(order.side, entry, stop_loss, take_profit)
        plan += self._earnings_check(order, now)
        return RiskReport(report.checks + tuple(plan), report.price, report.notional)

    def _earnings_check(self, order: OrderRequest, now: datetime) -> list[Check]:
        if self.earnings is None or order.side != "buy" or self.settings.risk.earnings_blackout_days <= 0:
            return []
        today = us_date(now)
        key = (order.symbol, today)
        if key not in self._earnings_seen:
            try:
                self._earnings_seen[key] = self.earnings(order.symbol, today)
            except Exception as exc:  # noqa: BLE001 - an unknown date is not a reason to crash
                logger.info("earnings lookup for %s failed: %s", order.symbol, exc)
                self._earnings_seen[key] = None
        return check_earnings(order.side, today, self._earnings_seen[key], self.settings.risk.earnings_blackout_days)

    def expire_stale(self) -> int:
        return self.store.expire(iso(self.clock()))

    def _notify(self, text: str) -> None:
        if self.notifier is None:
            return
        try:
            self.notifier(text)
        except Exception as exc:  # noqa: BLE001 - a failed ping must not lose the proposal
            logger.warning("notification failed: %s", exc)

    # -- what the agent may do --------------------------------------------

    def portfolio(self) -> dict[str, Any]:
        """Account, positions, limits and how much room is left today."""
        now = self.clock()
        self.expire_stale()
        account = self.broker.account()
        state = self._state(account, now, include_pending=True)
        limits = self.settings.risk
        equity = account.equity
        invested = state.invested
        buys = self._stops()
        # Only held stocks: a closed position's stop needs no trailing.
        stops = {
            p.symbol: (buys[p.symbol], *self._stop_of(buys[p.symbol], now))
            for p in account.positions
            if p.symbol in buys
        }
        return {
            "status": "ok",
            "paper_only": True,
            "account": {
                "acc_id": account.acc_id,
                "currency": account.currency,
                "equity": round(equity, 2),
                "cash": round(account.cash, 2),
                "invested": round(invested, 2),
                "invested_pct": round(100.0 * invested / equity, 2) if equity > 0 else None,
                "open_buy_orders": round(state.committed, 2) if account.open_buys is not None else None,
            },
            "positions": [
                {
                    "symbol": p.symbol,
                    "name": p.name,
                    "quantity": p.quantity,
                    "sellable": p.sellable,
                    "market_value": round(p.market_value, 2),
                    "weight_pct": round(100.0 * p.market_value / equity, 2) if equity > 0 else None,
                    "cost_price": p.cost_price,
                    "pl_pct": p.pl_pct,
                    "stop_loss": stops[p.symbol][1] if p.symbol in stops else None,
                    "stop_reason": stops[p.symbol][2] if p.symbol in stops else None,
                    "initial_stop": stops[p.symbol][0].stop_loss if p.symbol in stops else None,
                    "stop_from_proposal": stops[p.symbol][0].id if p.symbol in stops else None,
                }
                for p in account.positions
            ],
            "auto_stop": self.settings.exits.auto_stop,
            "limits": {
                "max_order_usd": round(equity * limits.max_order_pct / 100.0, 2),
                "max_position_usd": round(equity * limits.max_position_pct / 100.0, 2),
                "max_invested_usd": round(equity * limits.max_invested_pct / 100.0, 2),
                "max_buys_per_day": limits.max_buys_per_day,
                "max_pending": limits.max_pending,
                "min_price": limits.min_price,
                "allow_market_orders": limits.allow_market_orders,
                "blocked_symbols": list(limits.blocked_symbols),
                "stop_loss_required_for_buys": True,
            },
            "today": {
                "buys_placed": state.buys_today,
                "buys_left": max(limits.max_buys_per_day - state.buys_today, 0),
            },
            "pending_proposals": state.pending,
        }

    def propose(
        self,
        *,
        symbol: str,
        side: str,
        quantity: Any,
        thesis: str,
        playbook: str,
        order_type: str = "limit",
        limit_price: Any = None,
        stop_loss: Any = None,
        take_profit: Any = None,
        horizon: str = "",
        confidence: str = "",
    ) -> dict[str, Any]:
        """Check a proposed order and queue it for a person, or explain why not."""
        now = self.clock()
        self.expire_stale()
        thesis = str(thesis or "").strip()
        playbook = str(playbook or "").strip()
        try:
            if len(thesis) < _MIN_THESIS:
                raise ValueError(f"thesis must explain the trade in at least {_MIN_THESIS} characters")
            if not playbook:
                raise ValueError("playbook must name the investor skill(s) the idea came from")
            order = OrderRequest(
                symbol=normalize_symbol(symbol),
                side=str(side or "").strip().lower(),
                quantity=_whole_shares(quantity),
                order_type=str(order_type or "limit").strip().lower(),
                limit_price=_optional_float(limit_price, "limit_price"),
            )
            stop = _optional_float(stop_loss, "stop_loss")
            target = _optional_float(take_profit, "take_profit")
        except ValueError as exc:
            return {"status": "invalid", "error": str(exc)}

        try:
            account = self.broker.account()
        except BrokerError as exc:
            return {"status": "error", "error": str(exc)}

        report = self._report(order, account, now, stop_loss=stop, take_profit=target, include_pending=True)
        waiting = next((p for p in self.store.list([st.PENDING], limit=100) if p.symbol == order.symbol), None)
        if waiting is not None:
            clash = Check(
                "one_per_symbol",
                False,
                f"#{waiting.id} for {order.symbol} is already waiting for approval; withdraw it first if this replaces it",
            )
            report = RiskReport(report.checks + (clash,), report.price, report.notional)
        status = st.PENDING if report.passed else st.REFUSED
        expires = proposal_expiry(now, self.settings.proposals.ttl_hours)
        proposal = self.store.add(
            created_at=iso(now),
            expires_at=iso(expires),
            status=status,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            limit_price=order.limit_price,
            ref_price=report.price,
            notional=report.notional,
            thesis=thesis[:_MAX_THESIS],
            playbook=playbook[:200],
            stop_loss=stop,
            take_profit=target,
            horizon=str(horizon or "")[:100],
            confidence=str(confidence or "")[:100],
            risk_report=report.to_dict(),
            decision_note="" if report.passed else "refused by risk checks",
        )
        if not report.passed:
            return {
                "status": st.REFUSED,
                "proposal_id": proposal.id,
                "problems": [f"{c.name}: {c.detail}" for c in report.failures()],
                "next": "Not queued. Fix the problems (usually a smaller size or a stop) or drop the idea. "
                "Do not split an order to get around a limit.",
            }
        equity_pct = (
            f", {100.0 * report.notional / account.equity:.1f}% of equity"
            if report.notional and account.equity > 0
            else ""
        )
        stop_text = f" Stop ${stop:,.2f}." if stop else ""
        self._notify(
            f"Trading God (paper) proposal {describe(proposal)}{equity_pct}. Playbook: {playbook}.{stop_text} "
            f"Approve or reject at http://{self.settings.web.host}:{self.settings.web.port}"
        )
        return {
            "status": st.PENDING,
            "proposal_id": proposal.id,
            "summary": describe(proposal),
            **order_fields(proposal),
            "expires_at": proposal.expires_at,
            "next": "Queued for a person to approve. It is NOT placed yet; never report it as a trade.",
        }

    def withdraw(self, proposal_id: int, reason: str = "") -> dict[str, Any]:
        """Let the agent take back its own pending proposal."""
        if self.store.decide(
            int(proposal_id), st.WITHDRAWN, iso(self.clock()), f"withdrawn by agent: {reason}".strip()
        ):
            return {"status": st.WITHDRAWN, "proposal_id": int(proposal_id)}
        found = self.store.get(int(proposal_id))
        return {
            "status": "unchanged",
            "error": "not pending" if found else "no such proposal",
            "current_status": found.status if found else None,
        }

    def history(self, status: str = "", limit: int = 20) -> list[dict[str, Any]]:
        """Recent proposals with the person's decision notes, newest first."""
        self.expire_stale()
        statuses = [s.strip() for s in str(status or "").split(",") if s.strip()]
        rows = self.store.list(statuses or None, limit=max(1, min(int(limit), 100)))
        out = []
        for p in rows:
            failed = [
                f"{c['name']}: {c['detail']}" for c in p.risk_report.get("checks", []) if not c.get("passed", True)
            ]
            out.append(
                {
                    "id": p.id,
                    "created_at": p.created_at,
                    "status": p.status,
                    "order": describe(p),
                    **order_fields(p),
                    "playbook": p.playbook,
                    "thesis": p.thesis[:300],
                    "decided_at": p.decided_at,
                    "decision_note": p.decision_note,
                    "problems": failed,
                    "error": p.error,
                    "broker_order_id": p.broker_order_id,
                }
            )
        return out

    # -- what only a person may do ----------------------------------------

    def approve(self, proposal_id: int, note: str = "") -> dict[str, Any]:
        """Re-check a pending proposal against the live account and place it."""
        now = self.clock()
        self.expire_stale()
        proposal = self.store.get(int(proposal_id))
        if proposal is None:
            return {"status": "not_found", "error": f"no proposal #{proposal_id}"}
        if not self.store.claim(proposal.id, iso(now), note.strip() or "approved"):
            current = self.store.get(proposal.id)
            return {
                "status": "unchanged",
                "error": f"proposal #{proposal.id} is {current.status if current else 'gone'}, not pending",
            }

        order = OrderRequest(
            symbol=proposal.symbol,
            side=proposal.side,
            quantity=proposal.quantity,
            order_type=proposal.order_type,
            limit_price=proposal.limit_price,
        )
        try:
            account = self.broker.account()
        except BrokerError as exc:
            self.store.finish(proposal.id, st.FAILED, error=f"could not read the account: {exc}")
            return {"status": st.FAILED, "error": str(exc)}

        report = self._report(
            order,
            account,
            now,
            stop_loss=proposal.stop_loss,
            take_profit=proposal.take_profit,
            include_pending=False,
            exclude_id=proposal.id,
        )
        if not report.passed:
            problems = [f"{c.name}: {c.detail}" for c in report.failures()]
            self.store.finish(proposal.id, st.BLOCKED, error="; ".join(problems), risk_report=report.to_dict())
            return {"status": st.BLOCKED, "problems": problems}

        try:
            placed = self.broker.place_order(order, remark=f"trading-god #{proposal.id}")
        except BrokerError as exc:
            self.store.finish(proposal.id, st.FAILED, error=str(exc), risk_report=report.to_dict())
            return {"status": st.FAILED, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - the order may or may not have reached moomoo
            message = f"unexpected error while sending; check the moomoo app before retrying: {exc}"
            self.store.finish(proposal.id, st.FAILED, error=message, risk_report=report.to_dict())
            return {"status": st.FAILED, "error": message}

        self.store.finish(
            proposal.id,
            st.PLACED,
            broker_order_id=placed["order_id"],
            broker_response=placed,
            risk_report=report.to_dict(),
        )
        return {"status": st.PLACED, "order_id": placed["order_id"], "summary": describe(proposal)}

    def reject(self, proposal_id: int, note: str = "") -> dict[str, Any]:
        if self.store.decide(int(proposal_id), st.REJECTED, iso(self.clock()), note.strip()):
            return {"status": st.REJECTED, "proposal_id": int(proposal_id)}
        found = self.store.get(int(proposal_id))
        return {
            "status": "unchanged",
            "error": f"proposal #{proposal_id} is {found.status if found else 'missing'}, not pending",
        }

    # -- what the desk does on its own --------------------------------------

    def _stops(self) -> dict[str, st.Proposal]:
        """The latest placed buy with a stop, per symbol: the stop a person approved."""
        stops: dict[str, st.Proposal] = {}
        for p in self.store.list([st.PLACED], limit=500):
            if p.side == "buy" and p.stop_loss and p.symbol not in stops:
                stops[p.symbol] = p
        return stops

    def check_stops(self) -> list[dict[str, Any]]:
        """Sell, at market, every position whose price is at or below its stop.

        Runs only in regular market hours. Shares already in an open sell order are
        not sellable, so a sell that is still working is not sent twice.
        """
        exits = self.settings.exits
        now = self.clock()
        if not exits.auto_stop or not us_session_open(now):
            return []
        stops = self._stops()
        if not stops:
            return []
        account = self.broker.account()
        since = iso(now - timedelta(minutes=exits.retry_minutes))
        recent = {
            p.symbol for p in self.store.list(None, limit=200) if p.playbook == AUTO_STOP and p.created_at >= since
        }
        results = []
        for position in account.positions:
            buy = stops.get(position.symbol)
            quantity = int(position.sellable)
            if buy is None or quantity <= 0 or position.symbol in recent:
                continue
            stop, reason = self._stop_of(buy, now)
            price = self.prices(position.symbol)
            if price is None or price > stop:
                continue
            results.append(self._stop_out(buy, stop, reason, quantity, price, account, now))
        return results

    def _stop_of(self, buy: st.Proposal, now: datetime) -> tuple[float, str]:
        """A placed buy's stop now, and where it came from. It never sits below the approved one."""
        self._trail(buy, now)
        raised = self.store.raised_stop(buy.id)
        if raised and raised[0] > (buy.stop_loss or 0):
            return raised
        return float(buy.stop_loss or 0), "the stop approved with the buy"

    def _trail(self, buy: st.Proposal, now: datetime) -> None:
        """Once a day, raise the stop to the highest close since the buy minus N ATRs."""
        exits = self.settings.exits
        if self.bars is None or not exits.trails(buy.playbook):
            return
        today = us_date(now)
        tried = self._trail_tried.get(buy.id)
        if (buy.id, today) in self._trailed or (tried and now - tried < timedelta(minutes=30)):
            return
        self._trail_tried[buy.id] = now
        since = us_date(datetime.fromisoformat(buy.decided_at or buy.created_at))
        try:
            bars = self.bars(buy.symbol, since)
        except Exception as exc:  # noqa: BLE001 - keep the current stop and try again later
            logger.info("daily bars for %s failed: %s", buy.symbol, exc)
            return
        self._trailed.add((buy.id, today))
        found = trailing_stop(bars, since, today, exits.trail_atr_multiple, exits.trail_atr_days)
        if found is None:
            return
        stop, high, atr = found
        raised = self.store.raised_stop(buy.id)
        if stop > max(buy.stop_loss or 0, raised[0] if raised else 0):
            reason = f"trailing: highest close ${high:,.2f} minus {exits.trail_atr_multiple:g} x ATR ${atr:,.2f}"
            self.store.raise_stop(buy.id, stop, reason, iso(now))

    def raise_stop(self, *, symbol: str, stop: Any, reason: str) -> dict[str, Any]:
        """Let the agent move a held position's stop up to a level its playbook names."""
        now = self.clock()
        reason = str(reason or "").strip()
        try:
            code = normalize_symbol(symbol)
            new = _optional_float(stop, "stop")
            if new is None:
                raise ValueError("stop is required")
            if len(reason) < 20:
                raise ValueError(
                    "reason must name the level the stop goes under, such as a base low or the 50-day line"
                )
        except ValueError as exc:
            return {"status": "invalid", "error": str(exc)}
        buy = self._stops().get(code)
        if buy is None:
            return {"status": "refused", "error": f"no buy of {code} was placed through the desk, so it has no stop"}
        if not any(p.symbol == code and p.quantity > 0 for p in self.broker.account().positions):
            return {"status": "refused", "error": f"{code} is not held"}
        current, _ = self._stop_of(buy, now)
        if new <= current:
            return {
                "status": "refused",
                "error": f"${new:,.2f} is not above the current stop ${current:,.2f}; stops only go up",
                "stop_loss": current,
            }
        price = self.prices(code)
        if price is None:
            return {"status": "error", "error": f"no current price for {code}"}
        ceiling = round(price * (1 - _MIN_STOP_ROOM_PCT / 100.0), 2)
        if new > ceiling:
            return {
                "status": "refused",
                "error": f"${new:,.2f} is within {_MIN_STOP_ROOM_PCT:g}% of the last price ${price:,.2f}, so normal "
                "noise would sell it; to exit now, propose a sell instead",
                "last_price": price,
            }
        self.store.raise_stop(buy.id, new, f"raised by agent: {reason[:300]}", iso(now))
        return {"status": "ok", "symbol": code, "stop_loss": new, "previous_stop": current, "last_price": price}

    def _stop_out(
        self,
        buy: st.Proposal,
        stop: float,
        reason: str,
        quantity: int,
        price: float,
        account: Account,
        now: datetime,
    ) -> dict[str, Any]:
        for waiting in self.store.list([st.PENDING], limit=100):
            if waiting.symbol == buy.symbol:
                self.store.decide(
                    waiting.id, st.WITHDRAWN, iso(now), "withdrawn: the stop was hit and the desk sold the position"
                )
        order = OrderRequest(symbol=buy.symbol, side="sell", quantity=quantity, order_type="market")
        # A stop exits at market whatever desk.toml says about market buys.
        limits = replace(self.settings.risk, allow_market_orders=True)
        report = evaluate(order, price, self._state(account, now, include_pending=False), limits, include_pending=False)
        why = f"last price ${price:,.2f} is at or below the ${stop:,.2f} stop of #{buy.id} ({reason})"
        proposal = self.store.add(
            created_at=iso(now),
            expires_at=iso(now + timedelta(hours=1)),
            status=st.PENDING,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            order_type=order.order_type,
            ref_price=report.price,
            notional=report.notional,
            thesis=f"Automatic stop-loss sell: {why}.",
            playbook=AUTO_STOP,
            risk_report=report.to_dict(),
        )
        if not self.store.claim(proposal.id, iso(now), f"automatic stop-loss: {why}"):
            return {"status": "unchanged", "proposal_id": proposal.id, "symbol": order.symbol}
        result: dict[str, Any] = {"proposal_id": proposal.id, "symbol": order.symbol, "quantity": quantity}
        if not report.passed:
            problems = "; ".join(f"{c.name}: {c.detail}" for c in report.failures())
            self.store.finish(proposal.id, st.BLOCKED, error=problems)
            result.update(status=st.BLOCKED, error=problems)
        else:
            try:
                placed = self.broker.place_order(order, remark=f"trading-god #{proposal.id} stop")
            except Exception as exc:  # noqa: BLE001 - recorded, retried after exits.retry_minutes
                self.store.finish(proposal.id, st.FAILED, error=str(exc))
                result.update(status=st.FAILED, error=str(exc))
            else:
                self.store.finish(proposal.id, st.PLACED, broker_order_id=placed["order_id"], broker_response=placed)
                result.update(status=st.PLACED, order_id=placed["order_id"])
        if result["status"] == st.PLACED:
            self._notify(
                f"Trading God (paper) STOP HIT: sold {quantity} {order.symbol} at market, {why}. "
                f"No approval was needed. #{proposal.id}"
            )
        else:
            self._notify(
                f"Trading God (paper) STOP HIT on {order.symbol}, but the sell did not go through: "
                f"{result['error']}. Sell it in moomoo. #{proposal.id}"
            )
        return result


def watch_stops(desk: Desk, stop: threading.Event, report: Callable[[str], None] = print) -> None:
    """Check stops every ``exits.check_seconds`` until ``stop`` is set."""
    while not stop.wait(desk.settings.exits.check_seconds):
        try:
            for result in desk.check_stops():
                report(
                    f"{iso(desk.clock())} stop-loss {result.get('symbol')}: {result.get('status')} "
                    f"#{result.get('proposal_id')} {result.get('error', '')}".rstrip()
                )
        except BrokerError as exc:
            logger.info("stop check skipped: %s", exc)
        except Exception:  # noqa: BLE001 - the watcher must outlive one bad check
            logger.exception("stop check failed")
