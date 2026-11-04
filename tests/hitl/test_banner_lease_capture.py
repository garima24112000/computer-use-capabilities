"""In-page banner, lease enforcement at the seam, and capture redaction."""

import json
from pathlib import Path

import pytest

from cua.hitl.control import ControlNotHeld
from cua.hitl.escalation import EscalationRequest
from cua.hitl.hitl import HitlEscalator, HitlSession
from cua.hitl.interventions import InterventionStore
from cua.policy.guard import PolicyGuard
from cua.replay.session import run_replay
from cua.surface.locators import RoleName
from cua.surface.models import FrameRef
from tests.hitl.conftest import READ, Background, events, isolated_store, wait_for
from tests.hitl.test_handoff import HTTP500_AT_S04
from tests.surface.helpers import MAIN, NAV, goto_inquiry, login, spec

BANNER_Q = "() => document.querySelector('[data-cua-ignore]')?.textContent || null"
TOP_FRAME = FrameRef(name_path=["top"])  # the app's banner frame (TOP is the frameset itself)


@pytest.fixture
def session(make_guarded, policy, tmp_path):
    g = make_guarded()
    s = HitlSession(InterventionStore(tmp_path / "iv.db"), g.run, g.logger, g.redactor,
                    PolicyGuard(policy), headed=False, poll_ms=50)
    s.attach(g.surface)
    return g, s


def banner(g) -> str | None:
    return g.surface.inner.page.frame(name="top").evaluate(BANNER_Q)


def test_banner_for_each_state_and_the_scan_ignores_it(app_a, session):
    g, s = session
    login(g.surface, app_a, password="demo-pass-123")
    g.surface.inner.page.wait_for_timeout(300)  # repaint after the frameset loaded
    assert banner(g) == f"AUTOMATION IN CONTROL - run {g.run.run_id}"
    # The frameset document cannot render it; it sits in the app's own banner frame.
    obs = g.surface.observe()
    assert not any("IN CONTROL" in e.name for e in obs.elements)
    assert "IN CONTROL" not in obs.text_by_frame["top"]
    assert "IN CONTROL" not in g.surface.frame_text(TOP_FRAME)
    assert "IN CONTROL" not in g.surface.frame_text()
    assert g.surface.page_contains("LAKESHORE MUTUAL", TOP_FRAME)
    g.surface.wait_for_settle(5000)  # the banner does not keep the page "busy"

    s.control.transition("escalate", "automation", "test")
    assert banner(g).startswith("WAITING FOR AN OPERATOR")
    s.control.transition("take_control", "human:dana", owner="human:dana")
    assert banner(g) == ("HUMAN IN CONTROL - dana - click Resume in the operator console when "
                         "done")
    assert not any("IN CONTROL" in e.name for e in g.surface.observe().elements)
    s.control.transition("resume", "human:dana")
    s.control.transition("verified", "automation")
    assert banner(g).startswith("AUTOMATION IN CONTROL")


def test_automation_action_while_human_holds_lease_is_not_dispatched(app_a, session):
    g, s = session
    login(g.surface, app_a, password="demo-pass-123")
    s.control.transition("escalate", "automation", "test")
    s.control.transition("take_control", "human:dana", owner="human:dana")
    before = len(g.run.events_path.read_text().splitlines())
    sign_off = spec(TOP_FRAME, RoleName(role="link", name="SIGN OFF"))
    with pytest.raises(ControlNotHeld, match="human:dana"):
        g.surface.click(sign_off)
    with pytest.raises(ControlNotHeld):
        g.surface.open(f"{app_a}/logout")
    assert g.surface.page_contains("FUNCTIONS", NAV)  # still signed on: nothing dispatched
    new = [json.loads(x) for x in g.run.events_path.read_text().splitlines()[before:]]
    assert not [e for e in new if e["type"] == "action"]
    s.control.transition("resume", "human:dana")
    goto_inquiry(g.surface)  # automation holds the lease again
    assert g.surface.page_contains("MEMBER NO.", MAIN)


def test_human_click_on_commit_control_is_flagged_irreversible(app_a, session):
    g, s = session
    assert s._classify("button", "SUBMIT", "main") in ("irreversible", "risky")
    assert s._classify("button", "Transfer funds", "main") == "irreversible"
    assert s._classify("link", "NEW INQUIRY", "main") == "risky"


FORBIDDEN = ["demo-pass-123", "900-12-3456", "555-0101", "john.sample@example.com",
             "SAMPLE, JOHN Q", "100 TEST LANE"]


def test_human_typing_secrets_is_captured_without_values(app_a, tmp_path, console, human):
    caps = isolated_store(tmp_path, READ, "1.1.0")
    run = Background(lambda: run_replay(
        f"{READ}@1.1.0", {"member_id": "12345"}, base_url=app_a, runs_root=tmp_path / "runs",
        capabilities_root=caps, hitl=True, hitl_store=console.store, headless=True,
        injections=HTTP500_AT_S04))
    iv = console.wait_open("replay_failure")
    console.cmd(iv["id"], "claim", operator="erin")
    console.wait_control(iv["id"], "HUMAN")
    h = human(iv["live_view"]["cdp_url"])
    h.top.click("text=SIGN OFF")  # the human signs off and on again by hand
    h.page.wait_for_selector("text=SIGN ON")
    h.page.click("a:has-text('SIGN ON')")
    h.page.wait_for_selector("input[name=f2]")
    h.page.fill("input[name=f1]", "demo_operator")
    h.page.fill("input[name=f2]", "demo-pass-123")
    h.page.press("input[name=f2]", "Tab")
    wait_for(lambda: sum(a["type"] == "input" for a in console.get(iv["id"])["human_actions"])
             >= 2, 20, "two input actions")
    console.cmd(iv["id"], "abort", operator="erin")
    result = run.join()
    assert result.status == "failed"

    inputs = [a for a in console.get(iv["id"])["human_actions"] if a["type"] == "input"]
    by_name = {a["name"]: a["detail"] for a in inputs}
    assert by_name["PASSWORD"] == "value changed"  # never a value for a password field
    assert by_name["OPERATOR ID"] == "value changed to '[REDACTED:SECRET]'"
    clicks = [a for a in console.get(iv["id"])["human_actions"] if a["type"] == "click"]
    assert clicks[0]["name"] == "SIGN OFF" and clicks[0]["risk"] == "risky"
    human_events = [e for e in events(result.evidence_dir) if e["type"] == "human_action"]
    assert human_events

    db = Path(console.store.path)
    files = [p for p in Path(result.evidence_dir).rglob("*") if p.is_file()]
    files += [p for p in db.parent.iterdir() if p.is_file()]  # db, -wal, -shm
    for path in files:
        blob = path.read_bytes()
        for needle in FORBIDDEN + ["demo_operator"]:
            assert needle.encode() not in blob, f"{needle} leaked into {path}"


def test_escalation_error_stops_cleanly_instead_of_escalating_twice(session, monkeypatch):
    """An error after control left AUTOMATION must not propagate: the caller would escalate
    again from AWAITING_HUMAN (IllegalTransition) and the real error would be lost."""
    g, s = session

    def broken(_iid, *a, **k):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(s, "wait_for_resolution", broken)
    decision = HitlEscalator(s).escalate(EscalationRequest(
        reason="app_error", raised_by="replay", run_id=g.run.run_id, step_id="s04"))
    assert decision.action == "stop"
    assert "escalation failed: RuntimeError: store unavailable" in decision.note
    assert s.control.state == "DONE"
    [iv] = s.store.list()
    assert iv.status == "expired"
    errs = [e for e in events(g.run.dir) if e["type"] == "escalation_error"]
    assert errs and "store unavailable" in errs[0]["data"]["error"]
