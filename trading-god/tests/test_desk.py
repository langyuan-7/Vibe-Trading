from datetime import date, datetime, timezone

from conftest import START, buy

from trading_god import store as st
from trading_god.broker import BrokerError
from trading_god.desk import proposal_expiry


def test_valid_buy_is_queued_not_placed(desk, broker, notes):
    result = buy(desk)
    assert result["status"] == st.PENDING
    assert "NOT placed" in result["next"]
    assert broker.placed == []
    assert len(notes) == 1 and "#1 BUY 100 US.AAPL" in notes[0]
    proposal = desk.store.get(result["proposal_id"])
    assert (proposal.symbol, proposal.ref_price, proposal.notional) == ("US.AAPL", 200.0, 20_000.0)


def test_oversized_buy_is_refused_and_recorded(desk, notes):
    result = buy(desk, quantity=1000)  # $200,000 = 20% of equity
    assert result["status"] == st.REFUSED
    assert any(p.startswith("order_size") for p in result["problems"])
    assert "Do not split" in result["next"]
    assert desk.store.get(result["proposal_id"]).status == st.REFUSED
    assert notes == []


def test_buy_without_a_stop_is_refused(desk):
    result = buy(desk, stop=None)
    assert result["status"] == st.REFUSED
    assert any(p.startswith("stop_loss") for p in result["problems"])


def test_invalid_input_is_not_stored(desk):
    assert (
        desk.propose(symbol="AAPL", side="buy", quantity=10, thesis="short", playbook="x", limit_price=200.0)["status"]
        == "invalid"
    )
    assert buy(desk, quantity=2.5)["status"] == "invalid"
    assert buy(desk, limit="abc")["status"] == "invalid"
    assert desk.store.list() == []


def test_one_waiting_proposal_per_symbol(desk):
    first = buy(desk)
    second = buy(desk, quantity=50)
    assert second["status"] == st.REFUSED
    assert any(p.startswith("one_per_symbol") and f"#{first['proposal_id']}" in p for p in second["problems"])
    desk.withdraw(first["proposal_id"], "replaced")
    assert buy(desk, quantity=50)["status"] == st.PENDING


def test_broker_down_means_no_proposal(desk, broker):
    broker.account_error = BrokerError("moomoo OpenD is not listening")
    assert buy(desk) == {"status": "error", "error": "moomoo OpenD is not listening"}
    assert desk.store.list() == []


def test_approve_places_once(desk, broker):
    proposal_id = buy(desk)["proposal_id"]
    result = desk.approve(proposal_id, "looks right")
    assert result["status"] == st.PLACED and result["order_id"] == "ORD1"
    order, remark = broker.placed[0]
    assert (order.symbol, order.side, order.quantity, order.limit_price) == ("US.AAPL", "buy", 100, 200.0)
    assert remark == f"trading-god #{proposal_id}"
    placed = desk.store.get(proposal_id)
    assert (placed.status, placed.broker_order_id, placed.decision_note) == (st.PLACED, "ORD1", "looks right")
    assert desk.approve(proposal_id)["status"] == "unchanged"
    assert len(broker.placed) == 1


def test_approve_rechecks_against_the_account_now(desk, broker):
    proposal_id = buy(desk)["proposal_id"]
    broker.cash = 5_000.0  # spent elsewhere since the proposal
    result = desk.approve(proposal_id)
    assert result["status"] == st.BLOCKED
    assert any(p.startswith("cash") for p in result["problems"])
    assert broker.placed == []
    assert desk.store.get(proposal_id).status == st.BLOCKED


def test_approve_counts_orders_that_have_not_filled(desk, broker):
    first = buy(desk, symbol="AAPL", quantity=250)["proposal_id"]  # $50,000
    second = buy(desk, symbol="MSFT", quantity=100, limit=400.0, stop=370.0)["proposal_id"]
    assert desk.approve(first)["status"] == st.PLACED
    broker.open_buys = {"US.AAPL": 50_000.0}
    broker.cash = 80_000.0  # only $30,000 is still free
    result = desk.approve(second)
    assert result["status"] == st.BLOCKED and any(p.startswith("cash") for p in result["problems"])


def test_daily_buy_limit_allows_exactly_the_limit(make_desk, broker, prices):
    desk = make_desk(max_buys_per_day=2)
    for symbol in ("AAPL", "MSFT", "NVDA"):
        price = prices.prices[f"US.{symbol}"]
        buy(desk, symbol=symbol, quantity=10, limit=price, stop=price * 0.9)
    assert desk.approve(1)["status"] == st.PLACED
    assert desk.approve(2)["status"] == st.PLACED
    third = desk.approve(3)
    assert third["status"] == st.BLOCKED and any(p.startswith("daily_orders") for p in third["problems"])


def test_buy_limit_resets_on_the_next_new_york_day(make_desk, clock):
    desk = make_desk(max_buys_per_day=1)
    buy(desk)
    assert desk.approve(1)["status"] == st.PLACED
    clock.advance(hours=12, minutes=55)  # 22:55 in New York, the same day
    late = buy(desk, symbol="MSFT", quantity=10, limit=400.0, stop=370.0)
    assert late["status"] == st.REFUSED and any(p.startswith("daily_orders") for p in late["problems"])
    clock.advance(hours=1, minutes=10)  # 00:05 in New York, the next day
    fresh = buy(desk, symbol="MSFT", quantity=10, limit=400.0, stop=370.0)
    assert fresh["status"] == st.PENDING
    assert desk.approve(fresh["proposal_id"])["status"] == st.PLACED


def test_stop_loss_sells_go_through_after_the_buy_limit(make_desk, broker):
    desk = make_desk(max_buys_per_day=1)
    buy(desk)
    assert desk.approve(1)["status"] == st.PLACED
    broker.hold("US.AAPL", 100, 18_000.0)
    sell = desk.propose(
        symbol="AAPL",
        side="sell",
        quantity=100,
        thesis="Closed below the stop at $185; the setup failed.",
        playbook="minervini-vcp",
        limit_price=180.0,
    )
    assert sell["status"] == st.PENDING
    assert desk.approve(sell["proposal_id"])["status"] == st.PLACED


def test_cannot_sell_shares_already_in_a_sell_order(desk, broker):
    broker.hold("US.AAPL", 100, 20_000.0, sellable=0)
    result = desk.propose(
        symbol="AAPL",
        side="sell",
        quantity=100,
        thesis="Target reached at $240 after the earnings gap.",
        playbook="oneil-canslim",
        limit_price=200.0,
    )
    assert result["status"] == st.REFUSED and any(p.startswith("no_short") for p in result["problems"])


def test_expired_proposal_cannot_be_approved(desk, broker, clock):
    proposal_id = buy(desk)["proposal_id"]
    clock.advance(hours=12)
    assert desk.approve(proposal_id)["status"] == "unchanged"
    assert desk.store.get(proposal_id).status == st.EXPIRED
    assert broker.placed == []


def test_broker_failures_are_recorded(desk, broker):
    first = buy(desk)["proposal_id"]
    broker.place_error = BrokerError("moomoo place order failed: market closed")
    assert desk.approve(first) == {"status": st.FAILED, "error": "moomoo place order failed: market closed"}
    second = buy(desk, symbol="MSFT", quantity=10, limit=400.0, stop=370.0)["proposal_id"]
    broker.place_error = TimeoutError("socket timeout")
    result = desk.approve(second)
    assert result["status"] == st.FAILED and "check the moomoo app" in result["error"]
    assert desk.store.get(second).status == st.FAILED


def test_account_failure_at_approval_is_recorded(desk, broker):
    proposal_id = buy(desk)["proposal_id"]
    broker.account_error = BrokerError("OpenD logged out")
    assert desk.approve(proposal_id)["status"] == st.FAILED
    assert "OpenD logged out" in desk.store.get(proposal_id).error


def test_reject_and_withdraw_keep_the_note_for_the_agent(desk):
    first = buy(desk)["proposal_id"]
    second = buy(desk, symbol="MSFT", quantity=10, limit=400.0, stop=370.0)["proposal_id"]
    assert desk.reject(first, "too extended, wait for a pullback")["status"] == st.REJECTED
    assert desk.withdraw(second, "news changed")["status"] == st.WITHDRAWN
    assert desk.reject(first)["status"] == "unchanged"
    notes = {row["id"]: row["decision_note"] for row in desk.history("rejected,withdrawn")}
    assert notes == {first: "too extended, wait for a pullback", second: "withdrawn by agent: news changed"}


def test_history_shows_why_a_proposal_was_refused(desk):
    buy(desk, quantity=1000)
    (row,) = desk.history("refused")
    assert row["status"] == st.REFUSED and any(p.startswith("order_size") for p in row["problems"])


def test_portfolio_reports_limits_in_dollars(desk, broker):
    broker.hold("US.AAPL", 100, 20_000.0)
    broker.open_buys = {"US.MSFT": 4_000.0}
    buy(desk, symbol="NVDA", quantity=10, limit=150.0, stop=140.0)
    portfolio = desk.portfolio()
    assert portfolio["paper_only"] is True
    assert portfolio["limits"]["max_order_usd"] == 50_000.0
    assert portfolio["limits"]["max_position_usd"] == 100_000.0
    assert portfolio["account"]["open_buy_orders"] == 4_000.0
    assert portfolio["positions"][0]["weight_pct"] == 2.0
    assert portfolio["today"] == {"buys_placed": 0, "buys_left": 5}
    assert portfolio["pending_proposals"] == 1


def test_order_numbers_come_back_as_numbers(desk):
    # Vibe-Trading's grounding gate accepts only figures a tool returned as numbers.
    result = buy(desk, quantity=100, limit=199.53, stop=185.25)
    assert (result["limit_price"], result["stop_loss"], result["quantity"]) == (199.53, 185.25, 100)
    assert result["notional"] == 19953.0
    (row,) = desk.history("pending")
    assert (row["symbol"], row["limit_price"], row["stop_loss"], row["quantity"]) == ("US.AAPL", 199.53, 185.25, 100)


def test_buy_into_earnings_is_refused(desk):
    asked = []

    def earnings(symbol, today):
        asked.append((symbol, today))
        return {"US.AAPL": date(2026, 10, 29)}.get(symbol)

    desk.earnings = earnings
    result = buy(desk)
    assert result["status"] == st.REFUSED
    assert any(p.startswith("earnings") and "2026-10-29" in p for p in result["problems"])
    assert buy(desk, symbol="MSFT", quantity=50, limit=400.0, stop=370.0)["status"] == st.PENDING
    assert asked == [("US.AAPL", date(2026, 10, 27)), ("US.MSFT", date(2026, 10, 27))]


def test_earnings_lookup_failure_does_not_block(desk):
    def earnings(symbol, today):
        raise RuntimeError("Yahoo is down")

    desk.earnings = earnings
    assert buy(desk)["status"] == st.PENDING


def test_approval_rechecks_earnings(desk, broker):
    proposal_id = buy(desk)["proposal_id"]  # proposed before the date was known
    desk.earnings = lambda symbol, today: date(2026, 10, 28)
    result = desk.approve(proposal_id)
    assert result["status"] == st.BLOCKED
    assert any(p.startswith("earnings") for p in result["problems"])
    assert broker.placed == []


def test_morning_proposal_keeps_its_ttl():
    # 10:00 New York: 12 hours runs past today's close.
    assert proposal_expiry(START, 12) == datetime(2026, 10, 28, 2, 0, tzinfo=timezone.utc)


def test_evening_proposal_lasts_through_the_next_session():
    # 18:57 New York on Tuesday: open until Wednesday's 16:00 close.
    evening = datetime(2026, 10, 27, 22, 57, tzinfo=timezone.utc)
    assert proposal_expiry(evening, 12) == datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc)


def test_weekend_proposal_lasts_through_monday():
    friday_night = datetime(2026, 10, 31, 1, 0, tzinfo=timezone.utc)  # 21:00 Friday in New York
    assert proposal_expiry(friday_night, 12) == datetime(2026, 11, 2, 21, 0, tzinfo=timezone.utc)  # EST by then
