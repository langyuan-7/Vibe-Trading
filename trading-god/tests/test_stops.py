from dataclasses import replace
from datetime import datetime, timezone

import pytest
from conftest import buy

from trading_god import store as st
from trading_god.broker import BrokerError
from trading_god.config import ExitSettings, parse_settings
from trading_god.desk import AUTO_STOP, us_session_open, watch_stops


@pytest.fixture
def held(desk, broker, prices):
    """100 AAPL bought through the desk at 200 with a 185 stop."""
    proposal_id = buy(desk)["proposal_id"]
    assert desk.approve(proposal_id)["status"] == st.PLACED
    broker.hold("US.AAPL", 100, 20_000.0)
    return proposal_id


def auto_sells(desk):
    return [p for p in desk.store.list(None, limit=100) if p.playbook == AUTO_STOP]


def test_session_hours():
    assert us_session_open(datetime(2026, 10, 27, 13, 30, tzinfo=timezone.utc))  # 9:30 New York
    assert not us_session_open(datetime(2026, 10, 27, 13, 29, tzinfo=timezone.utc))
    assert not us_session_open(datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc))  # 16:00
    assert not us_session_open(datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc))  # Saturday


def test_stop_hit_sells_at_market_without_approval(desk, broker, prices, notes, held):
    prices.prices["US.AAPL"] = 184.5
    (result,) = desk.check_stops()
    assert result["status"] == st.PLACED and result["quantity"] == 100
    order, remark = broker.placed[-1]
    assert (order.symbol, order.side, order.quantity, order.order_type) == ("US.AAPL", "sell", 100, "market")
    assert "stop" in remark
    (sell,) = auto_sells(desk)
    assert sell.status == st.PLACED and f"#{held}" in sell.decision_note
    assert "STOP HIT: sold 100 US.AAPL" in notes[-1] and "No approval was needed" in notes[-1]


def test_market_orders_turned_off_do_not_block_a_stop(make_desk, broker, prices, held):
    desk = make_desk(allow_market_orders=False)
    prices.prices["US.AAPL"] = 180.0
    assert desk.check_stops()[0]["status"] == st.PLACED


def test_nothing_happens_above_the_stop_or_outside_market_hours(desk, broker, prices, clock, held):
    prices.prices["US.AAPL"] = 185.01
    assert desk.check_stops() == []
    prices.prices["US.AAPL"] = 170.0
    clock.now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)  # 8:00 New York, pre-market
    assert desk.check_stops() == []
    assert len(broker.placed) == 1  # only the buy


def test_turned_off_means_no_automatic_sell(desk, prices, held):
    desk.settings = replace(desk.settings, exits=ExitSettings(auto_stop=False))
    prices.prices["US.AAPL"] = 170.0
    assert desk.check_stops() == []


def test_one_try_per_retry_window(desk, broker, prices, clock, held):
    prices.prices["US.AAPL"] = 180.0
    assert len(desk.check_stops()) == 1
    clock.advance(minutes=1)
    assert desk.check_stops() == []
    clock.advance(minutes=30)
    assert len(desk.check_stops()) == 1  # still held, so the desk tries again


def test_shares_already_in_a_sell_order_are_not_sold_twice(desk, broker, prices, held):
    broker.positions.clear()
    broker.hold("US.AAPL", 100, 18_000.0, sellable=0)
    prices.prices["US.AAPL"] = 180.0
    assert desk.check_stops() == []


def test_positions_bought_outside_the_desk_are_left_alone(desk, broker, prices):
    broker.hold("US.MSFT", 10, 4_000.0)
    prices.prices["US.MSFT"] = 1.0
    assert desk.check_stops() == []


def test_waiting_proposals_for_the_stock_are_withdrawn(desk, prices, held):
    waiting = desk.propose(
        symbol="AAPL",
        side="sell",
        quantity=100,
        limit_price=199.0,
        thesis="Target reached, taking profits on the whole position now.",
        playbook="minervini-vcp",
    )
    assert waiting["status"] == st.PENDING
    prices.prices["US.AAPL"] = 180.0
    desk.check_stops()
    assert desk.store.get(waiting["proposal_id"]).status == st.WITHDRAWN


def test_a_failed_sell_is_recorded_and_reported(desk, broker, prices, notes, held):
    broker.place_error = BrokerError("market closed")
    prices.prices["US.AAPL"] = 180.0
    (result,) = desk.check_stops()
    assert result["status"] == st.FAILED
    assert auto_sells(desk)[0].error == "market closed"
    assert "did not go through" in notes[-1] and "Sell it in moomoo" in notes[-1]


def test_portfolio_shows_each_positions_stop(desk, held):
    (position,) = desk.portfolio()["positions"]
    assert (position["stop_loss"], position["stop_from_proposal"]) == (185.0, held)
    assert desk.portfolio()["auto_stop"] is True


def test_watcher_checks_until_told_to_stop(desk, prices, held):
    class TwoTicks:
        def __init__(self):
            self.calls = 0

        def wait(self, seconds):
            self.calls += 1
            return self.calls > 2

    prices.prices["US.AAPL"] = 180.0
    lines = []
    watch_stops(desk, TwoTicks(), lines.append)
    assert len(lines) == 1 and "US.AAPL: placed" in lines[0]


def test_exit_settings_are_validated():
    assert parse_settings({"exits": {"auto_stop": False}}).exits.auto_stop is False
    with pytest.raises(ValueError):
        parse_settings({"exits": {"check_seconds": 1}})


def test_desk_page_shows_the_stop(desk, held):
    from fastapi.testclient import TestClient

    from trading_god.web import create_app

    page = TestClient(create_app(desk), base_url="http://127.0.0.1:8765").get("/").text
    assert "<th>Stop</th>" in page and "<td>$185.00</td>" in page
    assert "sold at market, without asking" in page
