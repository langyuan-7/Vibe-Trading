"""MCP tools the Vibe-Trading agent uses to reach the approval desk.

Vibe-Trading starts this server over stdio and exposes the tools as
``mcp_desk_<tool>``. There is deliberately no tool to approve, place or cancel
an order: only a person can do that, on the desk page or with ``tg-desk``.
"""

from __future__ import annotations

import io
import logging
import os
import sys
from typing import Annotated, Any, Callable, Literal

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from trading_god.broker import BrokerError
from trading_god.desk import Desk

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Trading God approval desk for a moomoo PAPER trading account (US stocks and ETFs only). "
    "You can read the portfolio, propose orders and read back past proposals with the person's decisions. "
    "You cannot place, approve or cancel orders: a person approves every proposal. "
    "A proposal is not a trade; only list_proposals showing status 'placed' means an order was sent."
)

server = FastMCP("trading-god-desk", instructions=INSTRUCTIONS, log_level="WARNING")

_desk: Desk | None = None


def _get_desk() -> Desk:
    global _desk
    if _desk is None:
        from trading_god.factory import build_desk

        _desk = build_desk()
    return _desk


def _safely(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except BrokerError as exc:
        return {"status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - a tool must answer, not crash the server
        logger.exception("desk tool failed")
        return {"status": "error", "error": f"desk error: {exc}"}


@server.tool()
def get_portfolio() -> dict[str, Any]:
    """Paper account equity, cash, positions with weights, the hard risk limits in dollars,
    and how many buys are left today. Call this before proposing anything."""
    return _safely(lambda: _get_desk().portfolio())


@server.tool()
def propose_order(
    symbol: Annotated[str, Field(description="US stock or ETF ticker, e.g. AAPL, BRK.B or US.AAPL")],
    side: Literal["buy", "sell"],
    quantity: Annotated[int, Field(gt=0, description="Whole number of shares")],
    thesis: Annotated[
        str,
        Field(
            description="Why, in under 150 words: the setup, the evidence with numbers and dates from your "
            "tools, and what would prove it wrong"
        ),
    ],
    playbook: Annotated[str, Field(description="Investor skill(s) the idea came from, e.g. 'minervini-vcp + buffett'")],
    order_type: Literal["limit", "market"] = "limit",
    limit_price: Annotated[float | None, Field(description="Required for limit orders")] = None,
    stop_loss: Annotated[
        float | None, Field(description="Required for buys: the price that proves the idea wrong, below entry")
    ] = None,
    take_profit: Annotated[float | None, Field(description="Optional target price, above entry for buys")] = None,
    time_horizon: Annotated[str, Field(description="e.g. 'days-weeks' or '6-12 months'")] = "",
    confidence: Literal["low", "medium", "high"] | None = None,
) -> dict[str, Any]:
    """Queue one order for a person to approve. The desk checks it against the hard limits first and
    returns the problems if it fails. Nothing is placed until a person approves it."""
    return _safely(
        lambda: _get_desk().propose(
            symbol=symbol,
            side=side,
            quantity=quantity,
            thesis=thesis,
            playbook=playbook,
            order_type=order_type,
            limit_price=limit_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            horizon=time_horizon,
            confidence=confidence or "",
        )
    )


@server.tool()
def list_proposals(
    status: Annotated[
        str,
        Field(
            description="Optional comma-separated filter: pending, placed, rejected, refused, blocked, "
            "failed, expired, withdrawn"
        ),
    ] = "",
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    """Recent proposals, newest first, with what happened and the person's notes. Read the rejection
    notes before proposing again: do not re-propose a rejected idea without new evidence."""
    return _safely(lambda: {"status": "ok", "proposals": _get_desk().history(status, limit)})


@server.tool()
def withdraw_proposal(
    proposal_id: int,
    reason: Annotated[str, Field(description="Why the idea no longer holds")],
) -> dict[str, Any]:
    """Take back one of your pending proposals before a person decides on it."""
    return _safely(lambda: _get_desk().withdraw(proposal_id, reason))


def _reserve_stdout_for_protocol() -> io.BufferedWriter | None:
    """Give the MCP channel its own copy of stdout and send everything else to stderr.

    The futu SDK can print to stdout, and one stray line would corrupt the
    JSON-RPC stream, so file descriptor 1 and ``sys.stdout`` both point at
    stderr while the protocol writes to a duplicate of the original pipe.
    """
    try:
        protocol = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        sys.stdout = sys.stderr
        return protocol
    except (OSError, ValueError, AttributeError) as exc:
        logger.warning("could not separate stdout from the MCP channel: %s", exc)
        return None


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    protocol = _reserve_stdout_for_protocol()
    if protocol is None:
        server.run()
        return

    import anyio
    from mcp.server.stdio import stdio_server

    async def serve() -> None:
        stdout = anyio.wrap_file(io.TextIOWrapper(protocol, encoding="utf-8"))
        async with stdio_server(stdout=stdout) as (read_stream, write_stream):
            low_level = server._mcp_server
            await low_level.run(read_stream, write_stream, low_level.create_initialization_options())

    anyio.run(serve)


if __name__ == "__main__":
    main()
