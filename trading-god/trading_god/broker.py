"""moomoo paper-account adapter: the futu-api SDK talking to a local OpenD.

Every trade call passes ``TrdEnv.SIMULATE`` and the account is picked only from
OpenD's paper (SIMULATE) accounts, so there is no code path here that can reach
a live account. The adapter never calls ``unlock_trade``, which a live order
would need.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import socket
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any, Iterator

from trading_god.config import MoomooSettings
from trading_god.risk import OrderRequest

logger = logging.getLogger(__name__)

#: Preferred paper account kinds, best first. Futures and contest accounts are skipped.
_ACCOUNT_KIND_RANK = {"STOCK": 0, "STOCK_AND_OPTION": 1, "OPTION": 2, "NONE": 3, "N/A": 3}

#: Order states in which an order can still fill.
_OPEN_ORDER_STATUSES = frozenset(
    {"UNSUBMITTED", "WAITING_SUBMIT", "SUBMITTING", "SUBMITTED", "FILLED_PART", "CANCELLING_PART", "CANCELLING_ALL"}
)


class BrokerError(RuntimeError):
    """moomoo or OpenD refused a request, or could not be reached."""


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    quantity: float
    sellable: float
    market_value: float
    cost_price: float | None
    pl_pct: float | None


@dataclass(frozen=True)
class Account:
    acc_id: int
    currency: str
    equity: float
    cash: float
    buying_power: float
    market_value: float
    positions: tuple[Position, ...]
    #: Unfilled value of open buy orders by symbol; ``None`` when moomoo would not say.
    open_buys: dict[str, float] | None


def load_sdk() -> ModuleType:
    """Import futu-api (or moomoo-api, the same SDK under moomoo's name).

    The SDK's console logger writes to stdout, which would corrupt the desk's
    MCP stdio channel, so it is switched off here and kept to its log files.
    """
    with contextlib.redirect_stdout(sys.stderr):
        try:
            import futu as sdk  # type: ignore[import-not-found]
        except ModuleNotFoundError:
            try:
                import moomoo as sdk  # type: ignore[import-not-found]
            except ModuleNotFoundError as exc:
                raise BrokerError("futu-api is not installed. Run: pip install futu-api") from exc
    with contextlib.suppress(Exception):
        sdk.common.ft_logger.logger.enable_console_log(False)
    return sdk


def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rows(data: Any) -> list[dict[str, Any]]:
    """Turn an SDK payload (usually a pandas DataFrame) into plain dicts."""
    if data is None:
        return []
    if hasattr(data, "to_dict") and hasattr(data, "columns"):
        return list(data.to_dict("records"))
    if isinstance(data, dict):
        return [dict(data)]
    if isinstance(data, (list, tuple)):
        return [dict(item) for item in data if isinstance(item, dict)]
    return []


def _jsonable(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(row, default=str))


class MoomooPaperBroker:
    """US paper stock account behind moomoo OpenD."""

    def __init__(self, settings: MoomooSettings, sdk: ModuleType | None = None) -> None:
        self.settings = settings
        self._sdk = sdk

    @property
    def sdk(self) -> ModuleType:
        if self._sdk is None:
            self._sdk = load_sdk()
        return self._sdk

    # -- connection -------------------------------------------------------

    def gateway_open(self) -> bool:
        try:
            with socket.create_connection((self.settings.host, int(self.settings.port)), timeout=1.0):
                return True
        except OSError:
            return False

    def _require_gateway(self) -> None:
        if not self.gateway_open():
            raise BrokerError(
                f"moomoo OpenD is not listening on {self.settings.host}:{self.settings.port}. "
                "Open moomoo OpenD, log in, and leave it running."
            )

    @contextlib.contextmanager
    def _trade_context(self) -> Iterator[Any]:
        self._require_gateway()
        sdk = self.sdk
        market = getattr(sdk.TrdMarket, self.settings.market.upper(), None)
        firm = getattr(sdk.SecurityFirm, self.settings.security_firm.upper(), None)
        if market is None or firm is None:
            raise BrokerError(
                f"desk.toml has market={self.settings.market!r} and security_firm={self.settings.security_firm!r}; "
                'moomoo US is market = "US" and security_firm = "FUTUINC"'
            )
        ctx = sdk.OpenSecTradeContext(
            filter_trdmarket=market,
            host=self.settings.host,
            port=int(self.settings.port),
            security_firm=firm,
        )
        try:
            yield ctx
        finally:
            with contextlib.suppress(Exception):
                ctx.close()

    def _ok(self, result: Any, what: str) -> Any:
        ret, data = result[0], result[1]
        if ret != self.sdk.RET_OK:
            raise BrokerError(f"moomoo {what} failed: {data}")
        return data

    # -- accounts ---------------------------------------------------------

    def _all_accounts(self, ctx: Any) -> list[dict[str, Any]]:
        return _rows(self._ok(ctx.get_acc_list(), "account list"))

    def _paper_accounts(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        market = self.settings.market.upper()
        paper = []
        for row in rows:
            if str(row.get("trd_env", "")).upper() != "SIMULATE":
                continue
            auth = row.get("trdmarket_auth") or []
            markets = {str(item).upper() for item in ([auth] if isinstance(auth, str) else auth)}
            kind = str(row.get("sim_acc_type", "N/A")).upper()
            if market in markets and kind in _ACCOUNT_KIND_RANK:
                paper.append(row)
        paper.sort(key=lambda row: _ACCOUNT_KIND_RANK[str(row.get("sim_acc_type", "N/A")).upper()])
        return paper

    def _resolve_account(self, ctx: Any) -> int:
        paper = self._paper_accounts(self._all_accounts(ctx))
        wanted = int(self.settings.acc_id or 0)
        if wanted:
            if any(int(row["acc_id"]) == wanted for row in paper):
                return wanted
            raise BrokerError(
                f"acc_id {wanted} in desk.toml is not a {self.settings.market} paper account in OpenD. "
                "Set acc_id = 0 to pick the paper account automatically."
            )
        if not paper:
            raise BrokerError(
                f"OpenD reports no {self.settings.market} paper stock account. Open Paper Trading for "
                f"{self.settings.market} stocks once in the moomoo app, then try again."
            )
        return int(paper[0]["acc_id"])

    def describe_accounts(self) -> dict[str, Any]:
        """Everything ``tg-desk check`` shows: the gateway, every account OpenD sees, and the one in use."""
        report: dict[str, Any] = {
            "gateway": f"{self.settings.host}:{self.settings.port}",
            "gateway_open": self.gateway_open(),
        }
        if not report["gateway_open"]:
            report["error"] = "moomoo OpenD is not running or not logged in"
            return report
        with self._trade_context() as ctx:
            rows = self._all_accounts(ctx)
            report["accounts"] = [
                {
                    "acc_id": int(row.get("acc_id", 0)),
                    "trd_env": row.get("trd_env"),
                    "markets": row.get("trdmarket_auth"),
                    "kind": row.get("sim_acc_type"),
                    "security_firm": row.get("security_firm"),
                }
                for row in rows
            ]
            report["paper_account_in_use"] = self._resolve_account(ctx)
        return report

    def account(self) -> Account:
        sdk = self.sdk
        with self._trade_context() as ctx:
            acc_id = self._resolve_account(ctx)
            funds = _rows(
                self._ok(
                    ctx.accinfo_query(trd_env=sdk.TrdEnv.SIMULATE, acc_id=acc_id, currency=sdk.Currency.USD),
                    "account funds",
                )
            )
            holdings = _rows(self._ok(ctx.position_list_query(trd_env=sdk.TrdEnv.SIMULATE, acc_id=acc_id), "positions"))
            open_buys = self._open_buys(ctx, acc_id)
        info = funds[0] if funds else {}
        equity = _num(info.get("total_assets"))
        cash = _num(info.get("cash"))
        if cash is None:
            cash = _num(info.get("us_cash"))
        if equity is None or cash is None:
            raise BrokerError(f"moomoo did not report total assets and cash for paper account {acc_id}")
        currency = str(info.get("currency") or "USD").upper()
        positions = []
        for row in holdings:
            quantity = _num(row.get("qty")) or 0.0
            if quantity == 0:
                continue
            # Shares already in an open sell order are not sellable again.
            sellable = _num(row.get("can_sell_qty"))
            positions.append(
                Position(
                    symbol=str(row.get("code", "")).upper(),
                    name=str(row.get("stock_name", "")),
                    quantity=quantity,
                    sellable=quantity if sellable is None else sellable,
                    market_value=_num(row.get("market_val")) or 0.0,
                    cost_price=_num(row.get("cost_price")),
                    pl_pct=_num(row.get("pl_ratio")),
                )
            )
        return Account(
            acc_id=acc_id,
            currency="USD" if currency in ("", "N/A") else currency,
            equity=equity,
            cash=cash,
            buying_power=_num(info.get("power")) or 0.0,
            market_value=_num(info.get("market_val")) or 0.0,
            positions=tuple(positions),
            open_buys=open_buys,
        )

    def _open_buys(self, ctx: Any, acc_id: int) -> dict[str, float] | None:
        """Money already promised to buy orders that have not filled yet.

        Cash and positions move only when an order fills, so without this two
        approvals in a row would each see the same free cash.
        """
        try:
            rows = _rows(self._ok(ctx.order_list_query(trd_env=self.sdk.TrdEnv.SIMULATE, acc_id=acc_id), "order list"))
        except BrokerError as exc:
            logger.warning("could not read open orders: %s", exc)
            return None
        open_buys: dict[str, float] = {}
        for row in rows:
            if str(row.get("trd_side", "")).upper() not in ("BUY", "BUY_BACK"):
                continue
            if str(row.get("order_status", "")).upper() not in _OPEN_ORDER_STATUSES:
                continue
            remaining = (_num(row.get("qty")) or 0.0) - (_num(row.get("dealt_qty")) or 0.0)
            price = _num(row.get("price")) or 0.0
            if remaining > 0 and price > 0:
                symbol = str(row.get("code", "")).upper()
                open_buys[symbol] = open_buys.get(symbol, 0.0) + remaining * price
        return open_buys

    def recent_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        sdk = self.sdk
        with self._trade_context() as ctx:
            acc_id = self._resolve_account(ctx)
            rows = _rows(self._ok(ctx.order_list_query(trd_env=sdk.TrdEnv.SIMULATE, acc_id=acc_id), "order list"))
        keep = (
            "order_id",
            "code",
            "trd_side",
            "order_type",
            "order_status",
            "qty",
            "price",
            "dealt_qty",
            "dealt_avg_price",
            "create_time",
            "updated_time",
            "remark",
            "last_err_msg",
        )
        rows.sort(key=lambda row: str(row.get("create_time", "")), reverse=True)
        return [_jsonable({key: row.get(key) for key in keep if key in row}) for row in rows[:limit]]

    # -- quotes and orders ------------------------------------------------

    def last_price(self, symbol: str) -> float | None:
        """Latest price from moomoo, or ``None`` (no quote permission, OpenD down, unknown code)."""
        if not self.gateway_open():
            return None
        sdk = self.sdk
        try:
            ctx = sdk.OpenQuoteContext(host=self.settings.host, port=int(self.settings.port))
            try:
                ret, data = ctx.get_market_snapshot([symbol])
            finally:
                ctx.close()
        except Exception as exc:  # noqa: BLE001 - a missing quote falls back to Yahoo
            logger.info("moomoo quote for %s failed: %s", symbol, exc)
            return None
        if ret != sdk.RET_OK:
            logger.info("moomoo quote for %s refused: %s", symbol, data)
            return None
        rows = _rows(data)
        return _num(rows[0].get("last_price")) if rows else None

    def place_order(self, order: OrderRequest, remark: str) -> dict[str, Any]:
        """Send one order to the paper account and return moomoo's order id."""
        sdk = self.sdk
        if order.order_type == "limit":
            order_type, price = sdk.OrderType.NORMAL, float(order.limit_price or 0)
        else:
            order_type, price = sdk.OrderType.MARKET, 0.0
        side = sdk.TrdSide.BUY if order.side == "buy" else sdk.TrdSide.SELL
        with self._trade_context() as ctx:
            acc_id = self._resolve_account(ctx)
            data = self._ok(
                ctx.place_order(
                    price=price,
                    qty=int(order.quantity),
                    code=order.symbol,
                    trd_side=side,
                    order_type=order_type,
                    trd_env=sdk.TrdEnv.SIMULATE,
                    acc_id=acc_id,
                    remark=remark.encode("utf-8")[:64].decode("utf-8", "ignore"),
                    time_in_force=sdk.TimeInForce.DAY,
                ),
                "place order",
            )
        rows = _rows(data)
        order_id = str(rows[0].get("order_id", "")) if rows else ""
        if not order_id:
            raise BrokerError(f"moomoo accepted the request but returned no order id: {rows}")
        return {"order_id": order_id, "acc_id": acc_id, "moomoo": _jsonable(rows[0])}
