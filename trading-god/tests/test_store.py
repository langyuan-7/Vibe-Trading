import pytest

from trading_god import store as st
from trading_god.store import ProposalStore


def add(store, **overrides):
    values = dict(
        created_at="2026-10-27T14:00:00+00:00",
        expires_at="2026-10-28T02:00:00+00:00",
        status=st.PENDING,
        symbol="US.AAPL",
        side="buy",
        quantity=100,
        order_type="limit",
        limit_price=200.0,
        thesis="t" * 40,
        playbook="buffett",
        risk_report={"passed": True, "checks": []},
    )
    values.update(overrides)
    return store.add(**values)


@pytest.fixture
def store(tmp_path):
    return ProposalStore(tmp_path / "journal.db")


def test_add_get_list(store):
    first = add(store)
    second = add(store, symbol="US.MSFT", status=st.REFUSED)
    assert store.get(first.id).risk_report == {"passed": True, "checks": []}
    assert [p.id for p in store.list()] == [second.id, first.id]
    assert [p.id for p in store.list([st.PENDING])] == [first.id]
    assert store.count(st.PENDING) == 1
    assert store.get(999) is None


def test_add_rejects_unknown_columns(store):
    with pytest.raises(ValueError):
        add(store, status_override="placed")


def test_only_one_claim_wins(store):
    proposal = add(store)
    assert store.claim(proposal.id, "2026-10-27T15:00:00+00:00", "ok")
    assert not store.claim(proposal.id, "2026-10-27T15:00:01+00:00", "again")
    assert store.get(proposal.id).status == st.PLACING


def test_expired_proposals_cannot_be_claimed(store):
    proposal = add(store)
    assert not store.claim(proposal.id, "2026-10-28T02:00:00+00:00", "late")
    assert store.expire("2026-10-28T02:00:00+00:00") == 1
    assert store.get(proposal.id).status == st.EXPIRED


def test_decide_and_finish_only_move_forward(store):
    proposal = add(store)
    assert store.decide(proposal.id, st.REJECTED, "2026-10-27T15:00:00+00:00", "too extended")
    assert not store.decide(proposal.id, st.WITHDRAWN, "2026-10-27T15:00:00+00:00", "")
    store.finish(proposal.id, st.PLACED, broker_order_id="X")
    rejected = store.get(proposal.id)
    assert (rejected.status, rejected.decision_note, rejected.broker_order_id) == (st.REJECTED, "too extended", None)


def test_finish_records_the_broker_reply(store):
    proposal = add(store)
    store.claim(proposal.id, "2026-10-27T15:00:00+00:00", "go")
    store.finish(proposal.id, st.PLACED, broker_order_id="123", broker_response={"order_id": "123"})
    placed = store.get(proposal.id)
    assert (placed.status, placed.broker_order_id, placed.broker_response) == (st.PLACED, "123", {"order_id": "123"})


def test_buy_count_can_leave_out_the_proposal_being_approved(store):
    since = "2026-10-27T04:00:00+00:00"
    done = add(store)
    store.claim(done.id, "2026-10-27T15:00:00+00:00", "go")
    store.finish(done.id, st.PLACED, broker_order_id="1")
    current = add(store)
    store.claim(current.id, "2026-10-27T15:05:00+00:00", "go")
    add(store, side="sell", status=st.PLACED, decided_at="2026-10-27T15:00:00+00:00")
    add(store, status=st.PLACED, decided_at="2026-10-26T15:00:00+00:00")  # yesterday
    assert store.count_placed_buys_since(since) == 2
    assert store.count_placed_buys_since(since, exclude_id=current.id) == 1
