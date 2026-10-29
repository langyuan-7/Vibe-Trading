import sqlite3
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from conftest import buy
from fastapi.testclient import TestClient

from trading_god import setup_vibe
from trading_god import store as st
from trading_god.config import SwingSettings
from trading_god.desk import AUTO_TARGET, AUTO_TIME
from trading_god.risk import add_trading_days, check_swing_plan
from trading_god.web import create_app

LIMITS = {"min_days": 2, "max_days": 10, "min_reward_risk": 1.5}


def swing(desk, **extra):
    """Buy 100 AAPL at 200, stop 190, target 220, for at most 5 trading days."""
    values = {"stop": 190.0, "take_profit": 220.0, "max_hold_days": 5, "playbook": "darvas-box + oneil-canslim"}
    values.update(extra)
    return buy(desk, **values)


def failed(result):
    return {problem.split(":")[0] for problem in result.get("problems", [])}


def test_trading_days_skip_weekends():
    assert add_trading_days(date(2026, 10, 27), 5) == date(2026, 11, 3)
    assert add_trading_days(date(2026, 10, 30), 1) == date(2026, 11, 2)


def test_swing_plan_rules():
    assert check_swing_plan("buy", 200.0, 190.0, 220.0, None, **LIMITS) == []
    assert check_swing_plan("sell", 200.0, 190.0, 220.0, 5, **LIMITS) == []
    checks = {c.name: c.passed for c in check_swing_plan("buy", 200.0, 190.0, 220.0, 5, **LIMITS)}
    assert checks == {"swing_days": True, "reward_risk": True}
    assert not {c.name: c.passed for c in check_swing_plan("buy", 200.0, 190.0, 212.0, 5, **LIMITS)}["reward_risk"]
    assert not check_swing_plan("buy", 200.0, 190.0, 220.0, 11, **LIMITS)[0].passed
    assert [c.name for c in check_swing_plan("buy", 200.0, 190.0, None, 5, **LIMITS) if not c.passed] == [
        "swing_target"
    ]


def test_a_complete_swing_plan_is_queued(desk):
    result = swing(desk)
    assert result["status"] == st.PENDING and result["max_hold_days"] == 5
    assert desk.store.get(result["proposal_id"]).max_hold_days == 5


@pytest.mark.parametrize(
    ("extra", "problem"),
    [
        ({"take_profit": None}, "swing_target"),
        ({"take_profit": 210.0}, "reward_risk"),  # 1x the risk
        ({"max_hold_days": 15}, "swing_days"),
        ({"max_hold_days": 1}, "swing_days"),
    ],
)
def test_incomplete_swing_plans_are_refused(desk, extra, problem):
    result = swing(desk, **extra)
    assert result["status"] == st.REFUSED and problem in failed(result)


def test_sells_and_fractions_cannot_carry_hold_days(desk):
    assert swing(desk, max_hold_days=2.5)["status"] == "invalid"
    sell = desk.propose(
        symbol="AAPL",
        side="sell",
        quantity=10,
        limit_price=200.0,
        max_hold_days=5,
        thesis="Taking profits on part of the position into strength.",
        playbook="darvas-box",
    )
    assert sell["status"] == "invalid"


@pytest.fixture
def swing_held(desk, broker):
    proposal_id = swing(desk)["proposal_id"]
    assert desk.approve(proposal_id)["status"] == st.PLACED
    broker.hold("US.AAPL", 100, 20_000.0)
    return proposal_id


def test_target_hit_sells_without_approval(desk, broker, prices, notes, swing_held):
    prices.prices["US.AAPL"] = 220.5
    (result,) = desk.check_exits()
    assert (result["status"], result["kind"]) == (st.PLACED, AUTO_TARGET)
    order, _ = broker.placed[-1]
    assert (order.side, order.order_type, order.quantity) == ("sell", "market", 100)
    assert "TARGET HIT: sold 100 US.AAPL" in notes[-1]


def test_last_day_sells_near_the_close(desk, broker, prices, clock, swing_held):
    prices.prices["US.AAPL"] = 205.0
    clock.now = datetime(2026, 11, 2, 20, 50, tzinfo=timezone.utc)  # Monday 15:50, a day early
    assert desk.check_exits() == []
    clock.now = datetime(2026, 11, 3, 20, 30, tzinfo=timezone.utc)  # last day, 15:30
    assert desk.check_exits() == []
    clock.now = datetime(2026, 11, 3, 20, 50, tzinfo=timezone.utc)  # last day, 15:50
    (result,) = desk.check_exits()
    assert (result["status"], result["kind"]) == (st.PLACED, AUTO_TIME)
    assert "5-trading-day limit" in desk.store.get(result["proposal_id"]).decision_note


def test_automatic_target_and_time_exits_can_be_turned_off(desk, prices, clock, swing_held):
    desk.settings = replace(desk.settings, swing=SwingSettings(auto_target=False, auto_time_exit=False))
    prices.prices["US.AAPL"] = 230.0
    clock.now = datetime(2026, 11, 3, 20, 50, tzinfo=timezone.utc)
    assert desk.check_exits() == []
    prices.prices["US.AAPL"] = 185.0
    assert desk.check_exits()[0]["kind"] == "auto-stop"  # the stop still works


def test_ordinary_targets_still_wait_for_a_person(desk, broker, prices):
    proposal_id = buy(desk, take_profit=230.0)["proposal_id"]
    desk.approve(proposal_id)
    broker.hold("US.AAPL", 100, 20_000.0)
    prices.prices["US.AAPL"] = 240.0
    assert desk.check_exits() == []


def test_portfolio_and_page_show_the_swing_plan(desk, broker, swing_held):
    (position,) = desk.portfolio()["positions"]
    assert (position["target"], position["sell_by"]) == (220.0, "2026-11-03")
    assert desk.portfolio()["swing"]["max_hold_days"] == 10
    page = TestClient(create_app(desk), base_url="http://127.0.0.1:8765").get("/").text
    assert "swing: target $220.00, sell by 2026-11-03" in page


def test_card_says_approval_covers_the_exits(desk):
    swing(desk)
    page = TestClient(create_app(desk), base_url="http://127.0.0.1:8765").get("/").text
    assert "Approving this also approves its exits" in page and "trading day 5 after the buy" in page


def test_an_old_journal_gains_the_hold_days_column(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE proposals (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, status TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, "
            "quantity INTEGER NOT NULL, order_type TEXT NOT NULL, limit_price REAL, ref_price REAL, notional REAL, "
            "thesis TEXT NOT NULL DEFAULT '', playbook TEXT NOT NULL DEFAULT '', stop_loss REAL, take_profit REAL, "
            "horizon TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL DEFAULT '', risk_report TEXT NOT NULL "
            "DEFAULT '{}', decided_at TEXT, decision_note TEXT NOT NULL DEFAULT '', broker_order_id TEXT, "
            "broker_response TEXT, error TEXT)"
        )
        conn.execute(
            "INSERT INTO proposals (created_at, expires_at, status, symbol, side, quantity, order_type) "
            "VALUES ('2026-10-01', '2026-10-02', 'placed', 'US.MA', 'buy', 40, 'limit')"
        )
    store = st.ProposalStore(path)
    (old,) = store.list()
    assert old.symbol == "US.MA" and old.max_hold_days is None


def test_new_playbooks_are_installed_on_refresh(tmp_path):
    user = tmp_path / "skills"
    (user / "buffett").mkdir(parents=True)
    (user / "buffett" / "SKILL.md").write_text("installed")
    source = tmp_path / "download" / "investorskills-main" / "skills" / "darvas-box"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("---\nname: darvas-box\n---\nBuy the box breakout.\n")
    downloads = []

    def download(target):
        downloads.append(target)
        return tmp_path / "download" / "investorskills-main"

    assert setup_vibe.install_missing_playbooks(user, ["buffett", "darvas-box"], download) == ["darvas-box"]
    assert "Buy the box breakout" in (user / "darvas-box" / "SKILL.md").read_text()
    assert setup_vibe.install_missing_playbooks(user, ["buffett", "darvas-box"], download) == []
    assert len(downloads) == 1
