from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from conftest import buy

from trading_god import store as st
from trading_god.broker import BrokerError
from trading_god.config import ExitSettings, parse_settings
from trading_god.desk import AUTO_STOP, us_session_open, watch_exits
from trading_god.prices import Bar
from trading_god.risk import average_true_range, trailing_stop


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
    (result,) = desk.check_exits()
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
    assert desk.check_exits()[0]["status"] == st.PLACED


def test_nothing_happens_above_the_stop_or_outside_market_hours(desk, broker, prices, clock, held):
    prices.prices["US.AAPL"] = 185.01
    assert desk.check_exits() == []
    prices.prices["US.AAPL"] = 170.0
    clock.now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)  # 8:00 New York, pre-market
    assert desk.check_exits() == []
    assert len(broker.placed) == 1  # only the buy


def test_turned_off_means_no_automatic_sell(desk, prices, held):
    desk.settings = replace(desk.settings, exits=ExitSettings(auto_stop=False))
    prices.prices["US.AAPL"] = 170.0
    assert desk.check_exits() == []


def test_one_try_per_retry_window(desk, broker, prices, clock, held):
    prices.prices["US.AAPL"] = 180.0
    assert len(desk.check_exits()) == 1
    clock.advance(minutes=1)
    assert desk.check_exits() == []
    clock.advance(minutes=30)
    assert len(desk.check_exits()) == 1  # still held, so the desk tries again


def test_shares_already_in_a_sell_order_are_not_sold_twice(desk, broker, prices, held):
    broker.positions.clear()
    broker.hold("US.AAPL", 100, 18_000.0, sellable=0)
    prices.prices["US.AAPL"] = 180.0
    assert desk.check_exits() == []


def test_positions_bought_outside_the_desk_are_left_alone(desk, broker, prices):
    broker.hold("US.MSFT", 10, 4_000.0)
    prices.prices["US.MSFT"] = 1.0
    assert desk.check_exits() == []


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
    desk.check_exits()
    assert desk.store.get(waiting["proposal_id"]).status == st.WITHDRAWN


def test_a_failed_sell_is_recorded_and_reported(desk, broker, prices, notes, held):
    broker.place_error = BrokerError("market closed")
    prices.prices["US.AAPL"] = 180.0
    (result,) = desk.check_exits()
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
    watch_exits(desk, TwoTicks(), lines.append)
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


# -- trailing and raised stops ---------------------------------------------------

BUY_DAY = date(2026, 10, 27)


def bars_from(closes, start=date(2026, 9, 1), spread=2.0):
    """Daily bars on weekdays from ``start``, each 2 dollars from high to low."""
    out, day = [], start
    for close in closes:
        while day.weekday() >= 5:
            day += timedelta(days=1)
        out.append(Bar(day, close + spread / 2, close - spread / 2, close))
        day += timedelta(days=1)
    return out


def test_atr_needs_enough_bars():
    flat = bars_from([100.0] * 15)
    assert average_true_range(flat, 14) == 2.0
    assert average_true_range(flat[:14], 14) is None


def test_trail_is_highest_close_since_the_buy_minus_atr_and_skips_today():
    bars = bars_from([100.0] * 20 + [110.0, 120.0, 130.0], start=date(2026, 9, 1))
    since, today = bars[20].day, bars[-1].day
    stop, high, atr = trailing_stop(bars, since, today, 3.0, 14)
    assert high == 120.0  # today's 130 close is not in yet
    assert stop == round(120.0 - 3 * atr, 2)
    assert trailing_stop(bars, since, today, 0, 14) is None


@pytest.fixture
def trend_desk(make_desk, broker, prices, clock):
    """A Minervini buy of 100 AAPL at 200, stop 185, with daily bars on demand."""
    desk = make_desk()
    proposal_id = desk.propose(
        symbol="AAPL",
        side="buy",
        quantity=100,
        limit_price=200.0,
        stop_loss=185.0,
        thesis="Stage-2 leader breaking out of a tight VCP base on volume.",
        playbook="minervini-vcp + lynch-growth",
    )["proposal_id"]
    desk.approve(proposal_id)
    broker.hold("US.AAPL", 100, 20_000.0)
    desk.bars_calls = []

    def bars(symbol, since):
        desk.bars_calls.append((symbol, since))
        return desk.fake_bars

    desk.bars = bars
    desk.fake_bars = []
    desk.buy_id = proposal_id
    return desk


def test_stop_trails_up_and_never_down(trend_desk, prices, clock):
    # Fifteen calm days of history, then the stock runs from 200 to 240 after the buy.
    history = bars_from([200.0] * 15, start=date(2026, 10, 6))
    run = [Bar(BUY_DAY + timedelta(days=i), c + 1, c - 1, c) for i, c in enumerate([200.0, 220.0, 240.0], start=1)]
    trend_desk.fake_bars = history + run
    clock.now = datetime(2026, 11, 2, 15, 0, tzinfo=timezone.utc)  # Monday, after those closes
    prices.prices["US.AAPL"] = 238.0
    trail = round(240.0 - 3 * average_true_range(history + run, 14), 2)
    (position,) = trend_desk.portfolio()["positions"]
    assert 185.0 < position["stop_loss"] == trail
    assert position["stop_reason"].startswith("trailing") and position["initial_stop"] == 185.0
    # The next day the stock falls back; the trail does not come down with it.
    trend_desk.fake_bars = history + run + [Bar(date(2026, 11, 2), 231.0, 229.0, 230.0)]
    clock.now = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    assert trend_desk.portfolio()["positions"][0]["stop_loss"] == trail


def test_trailed_stop_is_what_the_watcher_sells_at(trend_desk, broker, prices, clock):
    history = bars_from([238.0] * 15, start=date(2026, 10, 6))
    trend_desk.fake_bars = history + [Bar(BUY_DAY + timedelta(days=1), 241.0, 239.0, 240.0)]
    clock.now = datetime(2026, 10, 29, 15, 0, tzinfo=timezone.utc)
    # ATR is 2.2, so the trail is 240 - 6.6 = 233.40: sell at 233, far above the 185 start.
    prices.prices["US.AAPL"] = 233.0
    (result,) = trend_desk.check_exits()
    assert result["status"] == st.PLACED
    assert "trailing" in trend_desk.store.get(result["proposal_id"]).decision_note


def test_bars_are_fetched_once_a_day(trend_desk, clock):
    trend_desk.fake_bars = bars_from([200.0] * 20, start=date(2026, 10, 1))
    trend_desk.portfolio()
    clock.advance(minutes=45)
    trend_desk.portfolio()
    assert len(trend_desk.bars_calls) == 1
    clock.advance(days=1)
    trend_desk.portfolio()
    assert len(trend_desk.bars_calls) == 2


def test_failed_bar_lookup_keeps_the_stop_and_retries_later(trend_desk, clock):
    def broken(symbol, since):
        trend_desk.bars_calls.append(symbol)
        raise RuntimeError("Yahoo is down")

    trend_desk.bars = broken
    assert trend_desk.portfolio()["positions"][0]["stop_loss"] == 185.0
    clock.advance(minutes=5)
    trend_desk.portfolio()
    clock.advance(minutes=31)
    trend_desk.portfolio()
    assert len(trend_desk.bars_calls) == 2


def test_thesis_playbooks_do_not_trail(desk, broker):
    proposal_id = buy(desk, playbook="buffett + lynch-growth")["proposal_id"]
    desk.approve(proposal_id)
    broker.hold("US.AAPL", 100, 20_000.0)
    desk.bars = lambda symbol, since: pytest.fail("a buffett + lynch buy must not fetch bars")
    (position,) = desk.portfolio()["positions"]
    assert position["stop_loss"] == 185.0


def test_agent_can_raise_a_stop_but_never_lower_it(trend_desk, prices):
    prices.prices["US.AAPL"] = 220.0
    raised = trend_desk.raise_stop(symbol="AAPL", stop=205.0, reason="under the new higher base low, minervini-vcp")
    assert (raised["status"], raised["stop_loss"], raised["previous_stop"]) == ("ok", 205.0, 185.0)
    lower = trend_desk.raise_stop(symbol="AAPL", stop=195.0, reason="under the 50-day line, minervini-vcp rule")
    assert lower["status"] == "refused" and "only go up" in lower["error"]
    assert trend_desk.portfolio()["positions"][0]["stop_reason"].startswith("raised by agent")


def test_raised_stop_must_leave_room_under_the_price(trend_desk, prices):
    prices.prices["US.AAPL"] = 220.0
    close = trend_desk.raise_stop(symbol="AAPL", stop=216.0, reason="under yesterday's low, minervini-vcp rule")
    assert close["status"] == "refused" and "2%" in close["error"]
    assert (
        trend_desk.raise_stop(symbol="AAPL", stop=215.6, reason="under the pivot, minervini-vcp rule")["status"] == "ok"
    )


def test_raise_stop_needs_a_desk_position_and_a_reason(trend_desk, desk, broker):
    assert trend_desk.raise_stop(symbol="AAPL", stop=190.0, reason="because")["status"] == "invalid"
    assert (
        trend_desk.raise_stop(symbol="NVDA", stop=140.0, reason="under the new base low, minervini")["status"]
        == "refused"
    )
    broker.positions.clear()
    gone = trend_desk.raise_stop(symbol="AAPL", stop=190.0, reason="under the new base low, minervini-vcp")
    assert gone["status"] == "refused" and "not held" in gone["error"]


def test_sold_positions_are_not_trailed(trend_desk, broker):
    broker.positions.clear()
    assert trend_desk.portfolio()["positions"] == []
    assert trend_desk.bars_calls == []
