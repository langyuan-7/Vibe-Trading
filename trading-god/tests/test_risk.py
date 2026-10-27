import pytest

from trading_god.risk import (
    AccountState,
    Holding,
    OrderRequest,
    RiskLimits,
    check_exit_plan,
    evaluate,
    normalize_symbol,
)

LIMITS = RiskLimits()
FRESH = AccountState(equity=1_000_000.0, cash=1_000_000.0)


def failed(report):
    return {check.name for check in report.failures()}


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("aapl", "US.AAPL"),
        (" AAPL ", "US.AAPL"),
        ("US.AAPL", "US.AAPL"),
        ("AAPL.US", "US.AAPL"),
        ("BRK-B", "US.BRK.B"),
        ("brk/b", "US.BRK.B"),
        ("US.BRK.B", "US.BRK.B"),
    ],
)
def test_normalize_symbol(raw, code):
    assert normalize_symbol(raw) == code


@pytest.mark.parametrize("symbol", ["US.AAPL261218C200000", "BTC-USDT", "HK.00700", "US.", "US.TOOLONGX"])
def test_only_plain_us_stock_codes_pass(symbol):
    order = OrderRequest(normalize_symbol(symbol) if symbol == "BTC-USDT" else symbol, "buy", 1, "limit", 100.0)
    assert "symbol" in failed(evaluate(order, 100.0, FRESH, LIMITS))


def test_limits_are_validated_and_blocked_symbols_normalized():
    with pytest.raises(ValueError):
        RiskLimits(max_order_pct=0)
    with pytest.raises(ValueError):
        RiskLimits(max_position_pct=150)
    with pytest.raises(ValueError):
        RiskLimits(max_pending=0)
    assert RiskLimits(blocked_symbols=("gme", "AMC.US")).blocked_symbols == ("US.GME", "US.AMC")


def test_buy_within_limits_passes():
    report = evaluate(OrderRequest("US.AAPL", "buy", 100, "limit", 200.0), 199.0, FRESH, LIMITS)
    assert report.passed, report.failures()
    assert report.notional == 20_000.0
    assert report.price == 199.0


def test_buy_over_the_order_cap_is_refused():
    # 300 x $200 = $60,000 = 6% of equity; the cap is 5%.
    report = evaluate(OrderRequest("US.AAPL", "buy", 300, "limit", 200.0), 200.0, FRESH, LIMITS)
    assert failed(report) == {"order_size"}


def test_position_cap_counts_holdings_and_open_buy_orders():
    account = AccountState(
        equity=1_000_000.0,
        cash=900_000.0,
        holdings={"US.AAPL": Holding("US.AAPL", 300, 60_000.0)},
        open_buys={"US.AAPL": 30_000.0},
    )
    # 60k held + 30k on order + 20k new = 11% > 10%.
    report = evaluate(OrderRequest("US.AAPL", "buy", 100, "limit", 200.0), 200.0, account, LIMITS)
    assert failed(report) == {"position_size"}


def test_cash_already_promised_to_open_orders_is_not_free():
    account = AccountState(equity=1_000_000.0, cash=50_000.0, open_buys={"US.MSFT": 40_000.0})
    report = evaluate(OrderRequest("US.AAPL", "buy", 100, "limit", 200.0), 200.0, account, LIMITS)
    assert "cash" in failed(report)


def test_unknown_open_orders_block_buys_but_not_sells():
    account = AccountState(
        equity=1_000_000.0, cash=1_000_000.0, holdings={"US.AAPL": Holding("US.AAPL", 100, 20_000.0)}, open_buys=None
    )
    assert "open_orders" in failed(evaluate(OrderRequest("US.AAPL", "buy", 10, "limit", 200.0), 200.0, account, LIMITS))
    assert evaluate(OrderRequest("US.AAPL", "sell", 100, "limit", 200.0), 200.0, account, LIMITS).passed


def test_invested_cap_keeps_cash_back():
    account = AccountState(
        equity=1_000_000.0,
        cash=120_000.0,
        holdings={f"US.S{i}": Holding(f"US.S{i}", 1, 88_000.0) for i in range(10)},
    )
    # 880k invested + 30k = 91% > 90%.
    report = evaluate(OrderRequest("US.AAPL", "buy", 150, "limit", 200.0), 200.0, account, LIMITS)
    assert failed(report) == {"invested"}


def test_daily_buy_count():
    account = AccountState(equity=1_000_000.0, cash=1_000_000.0, buys_today=5)
    report = evaluate(OrderRequest("US.AAPL", "buy", 10, "limit", 200.0), 200.0, account, LIMITS)
    assert failed(report) == {"daily_orders"}


def test_sells_are_only_checked_for_shorting():
    # Over every buy limit at once: a stop-loss exit must still go through.
    account = AccountState(
        equity=100_000.0,
        cash=0.0,
        holdings={"US.PENNY": Holding("US.PENNY", 50_000, 100_000.0)},
        buys_today=99,
    )
    report = evaluate(OrderRequest("US.PENNY", "sell", 50_000, "limit", 2.0), 2.0, account, LIMITS)
    assert report.passed, report.failures()


def test_cannot_sell_more_than_is_sellable():
    account = AccountState(equity=1_000_000.0, cash=0.0, holdings={"US.AAPL": Holding("US.AAPL", 40, 20_000.0)})
    assert failed(evaluate(OrderRequest("US.AAPL", "sell", 100, "limit", 200.0), 200.0, account, LIMITS)) == {
        "no_short"
    }
    assert failed(evaluate(OrderRequest("US.MSFT", "sell", 1, "limit", 400.0), 400.0, account, LIMITS)) == {"no_short"}


def test_penny_stocks_and_far_limits_are_refused():
    assert "min_price" in failed(evaluate(OrderRequest("US.PENNY", "buy", 100, "limit", 2.0), 2.0, FRESH, LIMITS))
    report = evaluate(OrderRequest("US.AAPL", "buy", 10, "limit", 240.0), 200.0, FRESH, LIMITS)
    assert failed(report) == {"limit_near_market"}


def test_market_orders_size_with_a_buffer_and_can_be_turned_off():
    report = evaluate(OrderRequest("US.AAPL", "buy", 100, "market"), 200.0, FRESH, LIMITS)
    assert report.passed
    assert report.notional == pytest.approx(20_200.0)
    off = RiskLimits(allow_market_orders=False)
    assert "order_type" in failed(evaluate(OrderRequest("US.AAPL", "buy", 100, "market"), 200.0, FRESH, off))


@pytest.mark.parametrize("quantity", [0, -5, True, 1.5])
def test_quantity_must_be_whole_and_positive(quantity):
    assert "quantity" in failed(
        evaluate(OrderRequest("US.AAPL", "buy", quantity, "limit", 200.0), 200.0, FRESH, LIMITS)
    )


def test_no_quote_stops_before_sizing():
    report = evaluate(OrderRequest("US.AAPL", "buy", 100, "limit", 200.0), None, FRESH, LIMITS)
    assert failed(report) == {"quote"}
    assert report.notional is None


def test_blocked_symbol_and_pending_queue():
    limits = RiskLimits(blocked_symbols=("AAPL",), max_pending=2)
    account = AccountState(equity=1_000_000.0, cash=1_000_000.0, pending=2)
    report = evaluate(OrderRequest("US.AAPL", "buy", 10, "limit", 200.0), 200.0, account, limits)
    assert failed(report) == {"not_blocked", "pending_queue"}
    approving = evaluate(
        OrderRequest("US.MSFT", "buy", 10, "limit", 400.0), 400.0, account, limits, include_pending=False
    )
    assert approving.passed


def test_exit_plan_requires_a_stop_below_entry_for_buys():
    assert [c.passed for c in check_exit_plan("buy", 200.0, None, None)] == [False]
    assert [c.passed for c in check_exit_plan("buy", 200.0, 205.0, None)] == [False]
    assert [c.passed for c in check_exit_plan("buy", 200.0, 185.0, 240.0)] == [True, True]
    assert [c.passed for c in check_exit_plan("buy", 200.0, 185.0, 190.0)] == [True, False]
    assert check_exit_plan("sell", 200.0, None, None) == []


def test_report_round_trips_to_json_friendly_dict():
    data = evaluate(OrderRequest("US.AAPL", "buy", 100, "limit", 200.0), 200.0, FRESH, LIMITS).to_dict()
    assert data["passed"] is True
    assert {"name", "passed", "detail"} <= set(data["checks"][0])
