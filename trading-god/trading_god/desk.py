"""The approval desk: the only path from the agent to the broker.

The agent can read the portfolio, propose an order and read back decisions.
Only a person, through the desk page or the ``tg-desk`` command, can approve a
proposal, and approval re-runs every risk check against fresh account state
before the order is sent to the paper account.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from trading_god import store as st
from trading_god.broker import Account, BrokerError, MoomooPaperBroker
from trading_god.config import Settings
from trading_god.prices import PriceLookup
from trading_god.risk import (
    AccountState,
    Check,
    Holding,
    OrderRequest,
    RiskReport,
    check_exit_plan,
    evaluate,
    normalize_symbol,
)

logger = logging.getLogger(__name__)

Notifier = Callable[[str], None]

_MAX_THESIS = 4000
_MIN_THESIS = 30


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
    ) -> None:
        self.settings = settings
        self.store = store
        self.broker = broker
        self.prices = prices
        self.notifier = notifier
        self.clock = clock

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
        return RiskReport(report.checks + tuple(plan), report.price, report.notional)

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
                }
                for p in account.positions
            ],
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
        expires = now + timedelta(hours=self.settings.proposals.ttl_hours)
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
                    "playbook": p.playbook,
                    "stop_loss": p.stop_loss,
                    "take_profit": p.take_profit,
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
