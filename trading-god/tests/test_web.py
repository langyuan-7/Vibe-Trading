import re

import pytest
from conftest import buy
from fastapi.testclient import TestClient

from trading_god import store as st
from trading_god.web import create_app

BASE = "http://127.0.0.1:8765"


@pytest.fixture
def client(desk):
    return TestClient(create_app(desk), base_url=BASE)


def token_of(page: str) -> str:
    return re.search(r'name="token" value="([^"]+)"', page).group(1)


def test_page_shows_the_account_and_waiting_proposals(client, desk):
    buy(desk)
    page = client.get("/").text
    assert "$1,000,000.00" in page
    assert "#1 BUY 100 US.AAPL limit $200.00" in page
    assert "Loss if stopped out: <b>$1,500.00</b>" in page
    assert re.search(r"All \d+ risk checks passed", page)
    assert "Approve and place" in page


def test_agent_text_is_escaped(client, desk):
    desk.propose(
        symbol="AAPL",
        side="buy",
        quantity=10,
        thesis="<script>alert('x')</script> plus enough words here",
        playbook="<b>buffett</b>",
        limit_price=200.0,
        stop_loss=180.0,
    )
    page = client.get("/").text
    assert "<script>alert" not in page
    assert "&lt;script&gt;alert" in page
    assert "<b>buffett</b>" not in page


def test_foreign_host_is_refused(desk):
    client = TestClient(create_app(desk), base_url="http://evil.example:8765")
    assert client.get("/").status_code == 403


def test_cross_site_post_is_refused(client, desk, broker):
    buy(desk)
    token = token_of(client.get("/").text)
    response = client.post(
        "/proposals/1/approve", data={"token": token}, headers={"Origin": "http://evil.example"}, follow_redirects=False
    )
    assert response.status_code == 403
    assert broker.placed == []


def test_wrong_token_does_nothing(client, desk, broker):
    buy(desk)
    response = client.post("/proposals/1/approve", data={"token": "guess"}, follow_redirects=False)
    assert response.status_code == 303 and "out%20of%20date" in response.headers["location"]
    assert broker.placed == []
    assert desk.store.get(1).status == st.PENDING


def test_approve_from_the_page(client, desk, broker):
    buy(desk)
    token = token_of(client.get("/").text)
    response = client.post(
        "/proposals/1/approve", data={"token": token, "note": "ok"}, headers={"Origin": BASE}, follow_redirects=False
    )
    assert response.status_code == 303
    assert len(broker.placed) == 1
    assert desk.store.get(1).status == st.PLACED
    assert "Placed" in client.get(response.headers["location"]).text


def test_reject_from_the_page_keeps_the_note(client, desk, broker):
    buy(desk)
    token = token_of(client.get("/").text)
    client.post("/proposals/1/reject", data={"token": token, "note": "earnings in two days"})
    rejected = desk.store.get(1)
    assert (rejected.status, rejected.decision_note) == (st.REJECTED, "earnings in two days")
    assert broker.placed == []


def test_page_still_loads_when_moomoo_is_down(client, desk, broker):
    from trading_god.broker import BrokerError

    broker.account_error = BrokerError("moomoo OpenD is not listening on 127.0.0.1:11111")
    page = client.get("/")
    assert page.status_code == 200
    assert "Cannot read the paper account" in page.text
