"""Pure tests: state machine, lease, intervention store (no browser)."""

import threading

import pytest

from cua.hitl.control import (
    TRANSITIONS,
    ControlNotHeld,
    ControlSession,
    ControlState,
    IllegalTransition,
)
from cua.hitl.interventions import InterventionRequest, InterventionStore, StaleState

S = ControlState


def test_every_legal_transition_and_epoch_increments():
    seen: list = []
    c = ControlSession("run1", [seen.append])
    assert (c.state, c.lease.owner, c.lease.epoch) == (S.AUTOMATION, "automation", 0)
    c.transition("escalate", "automation", "stuck")
    assert (c.state, c.lease.owner, c.lease.epoch) == (S.AWAITING_HUMAN, "nobody", 1)
    c.transition("take_control", "human:alice", owner="human:alice")
    assert (c.state, c.lease.owner, c.lease.epoch) == (S.HUMAN, "human:alice", 2)
    c.transition("resume", "human:alice")
    assert (c.state, c.lease.owner, c.lease.epoch) == (S.RESUMING, "automation", 3)
    c.transition("verification_failed", "automation")
    assert (c.state, c.lease.epoch) == (S.AWAITING_HUMAN, 4)
    c.transition("take_control", "human:bob", owner="human:bob")
    c.transition("resume", "human:bob")
    c.transition("verified", "automation")  # automation already held it: not a transfer
    assert (c.state, c.lease.owner, c.lease.epoch) == (S.AUTOMATION, "automation", 6)
    assert [t.event for t in seen] == ["escalate", "take_control", "resume",
                                       "verification_failed", "take_control", "resume",
                                       "verified"]
    assert seen[1].actor == "human:alice" and seen[0].reason == "stuck"
    for start in (S.AWAITING_HUMAN, S.HUMAN):
        for end in ("handled", "aborted", "expired"):
            assert TRANSITIONS[(start, end)] == S.DONE


@pytest.mark.parametrize("path", [["take_control"], ["resume"], ["verified"],
                                  ["escalate", "resume"], ["escalate", "escalate"],
                                  ["escalate", "aborted", "escalate"]])
def test_illegal_transitions_raise(path):
    c = ControlSession("run1")
    *ok, bad = path
    for event in ok:
        c.transition(event, "x", owner="human:x")
    with pytest.raises(IllegalTransition):
        c.transition(bad, "x", owner="human:x")


def test_take_control_requires_a_human_owner():
    c = ControlSession("run1")
    c.transition("escalate", "automation")
    with pytest.raises(ValueError):
        c.transition("take_control", "x", owner="automation")


def test_lease_blocks_automation_unless_it_holds_control():
    c = ControlSession("run1")
    c.require_automation("click x")
    c.transition("escalate", "automation")
    with pytest.raises(ControlNotHeld, match="held by nobody"):
        c.require_automation("click x")
    c.transition("take_control", "human:alice", owner="human:alice")
    with pytest.raises(ControlNotHeld, match="human:alice"):
        c.require_automation("click x")
    c.transition("resume", "human:alice")
    c.require_automation("click x")  # RESUMING: automation verifies the page


def test_two_concurrent_claims_exactly_one_wins(tmp_path):
    db = tmp_path / "iv.db"
    iv = InterventionStore(db).create(InterventionRequest(run_id="r", kind="replay_failure",
                                                          reason="x"))
    stores = [InterventionStore(db), InterventionStore(db)]  # like two console processes
    barrier = threading.Barrier(2)
    outcomes: list = []

    def claim(store, who):
        barrier.wait()
        try:
            store.claim(iv.id, who)
            outcomes.append(("won", who))
        except StaleState:
            outcomes.append(("lost", who))

    threads = [threading.Thread(target=claim, args=(s, n)) for s, n in zip(
        stores, ["alice", "bob"], strict=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(o for o, _ in outcomes) == ["lost", "won"]
    winner = next(w for o, w in outcomes if o == "won")
    assert InterventionStore(db).get(iv.id).operator == winner


def test_store_rules(tmp_path):
    store = InterventionStore(tmp_path / "iv.db")
    iv = store.create(InterventionRequest(run_id="r", kind="replay_failure", reason="x"))
    with pytest.raises(StaleState):
        store.resolve(iv.id, "resumed")  # resume needs a claim first
    store.claim(iv.id, "alice")
    with pytest.raises(StaleState):
        store.claim(iv.id, "bob")
    done = store.resolve(iv.id, "handled", note="did it by hand")
    assert (done.status, done.resolution, done.operator) == ("resolved", "handled", "alice")
    with pytest.raises(StaleState):
        store.expire(iv.id)
    store.set_control("r", "HUMAN", "human:alice", 2)
    assert store.control("r").owner == "human:alice"
