import re
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from trading_god.broker import BrokerError, MoomooPaperBroker
from trading_god.config import MoomooSettings
from trading_god.risk import OrderRequest

ROWS = [
    {"acc_id": 1, "trd_env": "REAL", "trdmarket_auth": ["US"], "sim_acc_type": "N/A"},
    {"acc_id": 2, "trd_env": "SIMULATE", "trdmarket_auth": ["US"], "sim_acc_type": "OPTION"},
    {"acc_id": 3, "trd_env": "SIMULATE", "trdmarket_auth": ["US"], "sim_acc_type": "STOCK"},
    {"acc_id": 4, "trd_env": "SIMULATE", "trdmarket_auth": ["HK"], "sim_acc_type": "STOCK"},
    {"acc_id": 5, "trd_env": "SIMULATE", "trdmarket_auth": ["US"], "sim_acc_type": "FUTURES"},
]


class FakeContext:
    def __init__(self, sdk, **kwargs):
        self.sdk = sdk
        sdk.contexts.append(kwargs)

    def get_acc_list(self):
        return 0, pd.DataFrame(ROWS)

    def accinfo_query(self, **kwargs):
        self.sdk.calls.append(("accinfo_query", kwargs))
        return 0, pd.DataFrame(
            [
                {
                    "total_assets": 1_000_000.0,
                    "cash": "N/A",
                    "us_cash": 950_000.0,
                    "power": 950_000.0,
                    "market_val": 50_000.0,
                    "currency": "USD",
                }
            ]
        )

    def position_list_query(self, **kwargs):
        self.sdk.calls.append(("position_list_query", kwargs))
        return 0, pd.DataFrame(
            [
                {
                    "code": "US.AAPL",
                    "stock_name": "Apple",
                    "qty": 100,
                    "can_sell_qty": 40,
                    "market_val": 20_000.0,
                    "cost_price": 180.0,
                    "pl_ratio": 11.1,
                },
                {
                    "code": "US.MSFT",
                    "stock_name": "Microsoft",
                    "qty": 50,
                    "can_sell_qty": "N/A",
                    "market_val": 20_000.0,
                    "cost_price": 390.0,
                    "pl_ratio": 2.5,
                },
                {
                    "code": "US.OLD",
                    "stock_name": "Sold out",
                    "qty": 0,
                    "can_sell_qty": 0,
                    "market_val": 0.0,
                    "cost_price": 0.0,
                    "pl_ratio": 0.0,
                },
            ]
        )

    def order_list_query(self, **kwargs):
        self.sdk.calls.append(("order_list_query", kwargs))
        if self.sdk.orders_fail:
            return -1, "busy"
        return 0, pd.DataFrame(
            [
                {
                    "code": "US.NVDA",
                    "trd_side": "BUY",
                    "order_status": "SUBMITTED",
                    "qty": 100,
                    "dealt_qty": 40,
                    "price": 150.0,
                },
                {
                    "code": "US.NVDA",
                    "trd_side": "BUY",
                    "order_status": "FILLED_ALL",
                    "qty": 10,
                    "dealt_qty": 10,
                    "price": 150.0,
                },
                {
                    "code": "US.AAPL",
                    "trd_side": "SELL",
                    "order_status": "SUBMITTED",
                    "qty": 60,
                    "dealt_qty": 0,
                    "price": 210.0,
                },
            ]
        )

    def place_order(self, **kwargs):
        self.sdk.calls.append(("place_order", kwargs))
        return 0, pd.DataFrame([{"order_id": "778899", "code": kwargs["code"]}])

    def close(self):
        pass


def fake_sdk():
    sdk = SimpleNamespace(
        RET_OK=0,
        TrdEnv=SimpleNamespace(SIMULATE="SIMULATE", REAL="REAL"),
        TrdMarket=SimpleNamespace(US="US", HK="HK"),
        SecurityFirm=SimpleNamespace(FUTUINC="FUTUINC", FUTUSECURITIES="FUTUSECURITIES"),
        Currency=SimpleNamespace(USD="USD"),
        OrderType=SimpleNamespace(NORMAL="NORMAL", MARKET="MARKET"),
        TrdSide=SimpleNamespace(BUY="BUY", SELL="SELL"),
        TimeInForce=SimpleNamespace(DAY="DAY"),
        contexts=[],
        calls=[],
        orders_fail=False,
    )
    sdk.OpenSecTradeContext = lambda **kwargs: FakeContext(sdk, **kwargs)
    return sdk


@pytest.fixture
def sdk():
    return fake_sdk()


@pytest.fixture
def broker(sdk, monkeypatch):
    broker = MoomooPaperBroker(MoomooSettings(), sdk=sdk)
    monkeypatch.setattr(broker, "gateway_open", lambda: True)
    return broker


def test_only_us_paper_stock_accounts_are_used(broker):
    assert [row["acc_id"] for row in broker._paper_accounts(ROWS)] == [3, 2]


def test_configured_account_must_be_a_paper_account(sdk, monkeypatch):
    broker = MoomooPaperBroker(MoomooSettings(acc_id=1), sdk=sdk)
    monkeypatch.setattr(broker, "gateway_open", lambda: True)
    with pytest.raises(BrokerError, match="not a US paper account"):
        broker.account()


def test_account_reads_the_paper_account(broker, sdk):
    account = broker.account()
    assert (account.acc_id, account.equity, account.cash) == (3, 1_000_000.0, 950_000.0)
    assert [(p.symbol, p.quantity, p.sellable) for p in account.positions] == [
        ("US.AAPL", 100, 40),
        ("US.MSFT", 50, 50),
    ]
    assert account.open_buys == {"US.NVDA": pytest.approx(9_000.0)}
    assert sdk.contexts == [{"filter_trdmarket": "US", "host": "127.0.0.1", "port": 11111, "security_firm": "FUTUINC"}]
    assert all(kwargs["trd_env"] == "SIMULATE" for _, kwargs in sdk.calls)


def test_open_orders_unknown_when_moomoo_will_not_say(broker, sdk):
    sdk.orders_fail = True
    assert broker.account().open_buys is None


def test_orders_go_to_the_paper_account_only(broker, sdk):
    result = broker.place_order(OrderRequest("US.AAPL", "buy", 10, "limit", 201.5), remark="trading-god #7" + "x" * 80)
    assert result["order_id"] == "778899" and result["acc_id"] == 3
    ((_, kwargs),) = [call for call in sdk.calls if call[0] == "place_order"]
    assert kwargs["trd_env"] == "SIMULATE"
    assert (kwargs["order_type"], kwargs["price"], kwargs["qty"], kwargs["trd_side"]) == ("NORMAL", 201.5, 10, "BUY")
    assert len(kwargs["remark"].encode("utf-8")) <= 64
    broker.place_order(OrderRequest("US.AAPL", "sell", 10, "market"), remark="exit")
    market = [call for call in sdk.calls if call[0] == "place_order"][-1][1]
    assert (market["order_type"], market["price"], market["trd_side"]) == ("MARKET", 0.0, "SELL")


def test_wrong_region_settings_are_explained(sdk, monkeypatch):
    broker = MoomooPaperBroker(MoomooSettings(security_firm="NOPE"), sdk=sdk)
    monkeypatch.setattr(broker, "gateway_open", lambda: True)
    with pytest.raises(BrokerError, match="FUTUINC"):
        broker.account()


def test_closed_gateway_is_a_clear_error(sdk):
    broker = MoomooPaperBroker(MoomooSettings(port=1), sdk=sdk)
    with pytest.raises(BrokerError, match="not listening"):
        broker.account()
    assert broker.last_price("US.AAPL") is None


def test_the_adapter_has_no_path_to_a_live_account():
    source = (Path(__file__).resolve().parents[1] / "trading_god" / "broker.py").read_text(encoding="utf-8")
    assert "unlock_trade(" not in source
    assert not re.search(r"TrdEnv\.REAL", source)
