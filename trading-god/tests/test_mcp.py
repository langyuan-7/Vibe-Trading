import json
import os
import sys

import anyio
import pytest
from conftest import buy

from trading_god import mcp_server
from trading_god.setup_vibe import DESK_TOOLS

WRITE_WORDS = ("approve", "place", "cancel", "modify", "unlock", "execute")


def tool_names() -> set[str]:
    return {tool.name for tool in anyio.run(mcp_server.server.list_tools)}


def test_agent_gets_exactly_the_desk_tools():
    assert tool_names() == set(DESK_TOOLS)


def test_no_tool_can_approve_or_place():
    for name in tool_names():
        assert not any(word in name for word in WRITE_WORDS), name


@pytest.fixture
def served_desk(desk, monkeypatch):
    monkeypatch.setattr(mcp_server, "_desk", desk)
    return desk


def test_propose_tool_only_queues(served_desk, broker):
    result = mcp_server.propose_order(
        symbol="AAPL",
        side="buy",
        quantity=100,
        thesis="Breakout from a seven-week base on rising volume.",
        playbook="minervini-vcp",
        limit_price=200.0,
        stop_loss=185.0,
        confidence="medium",
    )
    assert result["status"] == "pending"
    assert broker.placed == []
    listed = mcp_server.list_proposals(status="pending")
    assert [p["id"] for p in listed["proposals"]] == [result["proposal_id"]]
    assert mcp_server.withdraw_proposal(result["proposal_id"], "changed my mind")["status"] == "withdrawn"


def test_tool_errors_come_back_as_data(served_desk, broker):
    from trading_god.broker import BrokerError

    broker.account_error = BrokerError("OpenD logged out")
    assert mcp_server.get_portfolio() == {"status": "error", "error": "OpenD logged out"}
    broker.account_error = KeyError("boom")
    assert mcp_server.get_portfolio()["status"] == "error"


def test_history_tool_shows_the_persons_notes(served_desk):
    proposal_id = buy(served_desk)["proposal_id"]
    served_desk.reject(proposal_id, "earnings tomorrow")
    (row,) = mcp_server.list_proposals(status="rejected")["proposals"]
    assert row["decision_note"] == "earnings tomorrow"


def test_stdio_server_speaks_clean_json_rpc(tmp_path):
    """Run the server the way Vibe-Trading does: a child process over stdin/stdout."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    home = tmp_path / "tg"
    home.mkdir()
    # Point at a closed port, so a real OpenD on this machine is never touched.
    (home / "desk.toml").write_text("[moomoo]\nport = 1\n", encoding="utf-8")
    env = {**os.environ, "TRADING_GOD_HOME": str(home), "PYTHONUNBUFFERED": "1"}
    # If the server hangs, it prints every thread's stack to stderr and exits, so a
    # failure says where it was stuck.
    launch = (
        "import faulthandler, runpy; faulthandler.dump_traceback_later(40, exit=True); "
        "runpy.run_module('trading_god.mcp_server', run_name='__main__')"
    )
    params = StdioServerParameters(command=sys.executable, args=["-c", launch], env=env)

    log_path = tmp_path / "server-stderr.log"
    steps = []

    async def session_run():
        with anyio.fail_after(60), log_path.open("w", encoding="utf-8") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                steps.append("spawned")
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    steps.append("initialized")
                    tools = await session.list_tools()
                    steps.append("listed tools")
                    listed = await session.call_tool("list_proposals", {"status": "pending"})
                    steps.append("called list_proposals")
                    portfolio = await session.call_tool("get_portfolio", {})
                    steps.append("called get_portfolio")
                    return tools, listed, portfolio

    try:
        tools, listed, portfolio = anyio.run(session_run)
    except Exception as exc:
        pytest.fail(
            f"desk MCP server did not answer ({exc!r}) after steps {steps}; its stderr was:\n"
            + log_path.read_text(encoding="utf-8", errors="replace")
        )
    assert {t.name for t in tools.tools} == set(DESK_TOOLS)
    assert json.loads(listed.content[0].text) == {"status": "ok", "proposals": []}
    assert "OpenD is not listening on 127.0.0.1:1" in json.loads(portfolio.content[0].text)["error"]
