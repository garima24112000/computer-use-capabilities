"""End-to-end live-session handoff: replay fails → operator takes the SAME browser → human fixes
it on the raw page → Resume → replay verifies and continues."""

from pathlib import Path

import pytest

from cua.replay.harness import FaultInjection as F
from cua.replay.session import run_replay
from tests.hitl.conftest import (
    READ,
    ROOT,
    Background,
    cookie_digest,
    events,
    isolated_store,
    wait_for,
)

HTTP500_AT_S04 = [F(faults={"http500_count": 1, "http500_path_prefix": "/member"},
                    at_step="s04_click_name")]


def start_replay(app_a, tmp_path, console, injections=None, **kw):
    caps = isolated_store(tmp_path, READ, "1.1.0")
    return Background(lambda: run_replay(
        f"{READ}@1.1.0", {"member_id": "12345"}, base_url=app_a, runs_root=tmp_path / "runs",
        capabilities_root=caps, hitl=True, hitl_store=console.store, headless=True,
        injections=injections or HTTP500_AT_S04, **kw))


def redo_lookup_as_human(h, member: str = "12345") -> None:
    """What a person would do in the browser window after the 0xC0FFEE error page."""
    h.nav.click("text=MEMBER INQUIRY")
    h.main.wait_for_selector("input[name=txtA]")
    h.main.fill("input[name=txtA]", member)
    h.main.press("input[name=txtA]", "Tab")  # commit the change event
    h.main.click("a:has-text('INQUIRE')")
    h.main.wait_for_selector("a[href^='javascript:openMbr']")
    h.main.click("a[href^='javascript:openMbr']")
    h.main.wait_for_selector(f"text=MEMBER DETAIL - {member}")


def test_replay_handoff_resume_continues_with_same_session(app_a, tmp_path, console, human):
    run = start_replay(app_a, tmp_path, console)

    iv = console.wait_open("replay_failure")
    assert iv["capability"] == f"{READ}@1.1.0" and iv["step_id"] == "s04_click_name"
    assert iv["failure_category"] == "app_error" and "SYSTEM ERROR" not in (iv["expected"] or "")
    assert "system_error_coffee" in iv["observed"]
    assert iv["frame_urls"]["main"] == "/member/detail"
    assert Path(iv["screenshot"]).exists()
    assert console.http.get(f"/i/{iv['id']}/screenshot.png").status_code == 200
    assert iv["live_view"]["cdp_url"].startswith("http://127.0.0.1:")
    assert console.get(iv["id"])["control"]["state"] == "AWAITING_HUMAN"

    assert console.cmd(iv["id"], "claim", operator="alice").status_code == 200
    assert console.cmd(iv["id"], "claim", operator="bob").status_code == 409  # one owner
    control = console.wait_control(iv["id"], "HUMAN")
    assert control["owner"] == "human:alice"

    h = human(iv["live_view"]["cdp_url"])
    # Read cookies the way DevTools does (an attached client's context.cookies() is empty for
    # contexts it did not create); includes the httpOnly MCSID session cookie.
    cdp = h.context.new_cdp_session(h.page)
    raw = cdp.send("Network.getCookies", {"urls": [app_a]})["cookies"]
    assert [c["name"] for c in raw] == ["MCSID"]
    cookies_during = cookie_digest(raw)
    banner = h.top.evaluate("() => document.querySelector('[data-cua-ignore]')?.textContent")
    assert banner.startswith("HUMAN IN CONTROL - alice")
    for frame in (h.main, h.top, h.main):  # clicks on empty frame areas: noise, not actions
        frame.dispatch_event("body", "click")
    wait_for(lambda: console.get(iv["id"])["ignored_clicks"] == 3, 20, "ignored clicks")
    assert console.get(iv["id"])["human_actions"] == []
    redo_lookup_as_human(h)
    wait_for(lambda: len(console.get(iv["id"])["human_actions"]) >= 6, 20, "human actions")
    wait_for(lambda: (Path(iv["run_dir"]) / "live.png").exists(), 10, "live.png")
    assert console.http.get(f"/i/{iv['id']}/live.png").status_code == 200
    _render_console_example(console, iv["id"], h)

    assert console.cmd(iv["id"], "resume", operator="alice").status_code == 200
    result = run.join()

    assert result.status == "success", result.failure
    assert result.outputs == {"savings_balance": "12345.67"}
    [rec] = [r for r in result.recoveries if r.kind == "human_intervention"]
    assert rec.id == iv["id"] and "operator alice" in rec.detail
    # Rule order is fixed: the human ended on the member detail page, where the WHOLE success
    # checkpoint already holds, so rule (1) applies and only the Extract step runs.
    assert "rule success_checkpoint" in rec.detail
    assert [s.status for s in result.steps] == ["ok", "ok", "ok", "human", "ok"]
    assert result.irreversible_executed_by_human is False

    final = console.get(iv["id"])
    assert (final["status"], final["resolution"]) == ("resolved", "resumed")
    kinds = [a["type"] for a in final["human_actions"]]
    assert kinds.count("click") >= 3 and "input" in kinds and "navigate" in kinds
    clicks = [a for a in final["human_actions"] if a["type"] == "click"]
    assert all(a["name"] for a in clicks) and final["ignored_clicks"] == 3
    assert any(a["name"] == "INQUIRE" and a["role"] == "link" for a in clicks)
    assert any(a["name"] == "[REDACTED:NAME]" for a in clicks)  # the member's name: redacted
    typed = next(a for a in final["human_actions"] if a["type"] == "input")
    assert typed["name"] == "MEMBER NO." and "12345" in typed["detail"]

    checks = [e["data"] for e in events(result.evidence_dir) if e["type"] == "session_check"]
    assert [c["stage"] for c in checks] == ["escalated", "human_in_control", "resumed"]
    assert len({c["context_id"] for c in checks}) == 1  # never a new context/page
    assert len({c["page_id"] for c in checks}) == 1
    assert {c["cookie_sha256_16"] for c in checks} == {cookies_during}  # same session cookie
    transitions = [e["data"]["event"] for e in events(result.evidence_dir)
                   if e["type"] == "control_transition"]
    assert transitions == ["escalate", "take_control", "resume", "verified"]
    human_events = [e for e in events(result.evidence_dir) if e["type"] == "human_action"]
    assert human_events and all(e["actor"] == "human" for e in human_events)
    assert all(e["data"]["operator"] == "alice" for e in human_events)


def _render_console_example(console, iid: str, h) -> None:
    """Screenshot of the operator console detail page (for the README/report)."""
    from cua.surface.web.playwright_surface import _SharedDriver

    pw = _SharedDriver.acquire()
    try:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 1400})
        page.goto(f"{console.base}/i/{iid}")
        page.wait_for_timeout(500)
        out = ROOT / "runs" / "_hitl" / "console_example.png"
        out.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out), full_page=True)
        browser.close()
    finally:
        _SharedDriver.release()


def test_resume_verification_failure_reescalates_then_fails(app_a, tmp_path, console):
    run = start_replay(app_a, tmp_path, console)
    first = console.wait_open("replay_failure")
    console.cmd(first["id"], "claim", operator="alice")
    console.wait_control(first["id"], "HUMAN")
    console.cmd(first["id"], "resume", operator="alice")  # the human did nothing useful

    second = console.wait_open("replay_failure", exclude={first["id"]})
    assert "resume verification failed (round 1)" in second["reason"]
    assert console.get(second["id"])["control"]["state"] == "AWAITING_HUMAN"
    console.cmd(second["id"], "claim", operator="bob")
    console.wait_control(second["id"], "HUMAN")
    console.cmd(second["id"], "resume", operator="bob")

    result = run.join()
    assert result.status == "failed"
    assert "after 2 operator rounds" in result.failure.observed
    recs = [r for r in result.recoveries if r.kind == "human_intervention"]
    assert len(recs) == 2 and all("verification failed" in r.detail for r in recs)
    assert {i["id"] for i in console.list("resolved")} == {first["id"], second["id"]}


def test_no_operator_expires_and_stops(app_a, tmp_path, console):
    run = start_replay(app_a, tmp_path, console, hitl_timeout_s=1.5)
    result = run.join()
    assert result.status == "failed" and result.failure.category == "app_error"
    assert result.failure.escalation["resolution"] == "expired"
    [iv] = console.list("expired")
    assert console.store.control(iv["run_id"]).state == "DONE"


@pytest.mark.parametrize("cmd", ["abort"])
def test_abort_stops_the_run(app_a, tmp_path, console, cmd):
    run = start_replay(app_a, tmp_path, console)
    iv = console.wait_open()
    assert console.cmd(iv["id"], "handled").status_code == 400  # a note is required
    console.cmd(iv["id"], cmd, operator="alice")
    result = run.join()
    assert result.status == "failed" and result.failure.escalation["action"] == "stop"


@pytest.mark.parametrize("scenario", ["expect_holds", "retry_step"])
def test_resume_rules_2_and_3(app_a, tmp_path, console, human, scenario):
    if scenario == "expect_holds":  # s01 fails; the human just opens the inquiry screen
        inject = [F(faults={"http500_count": 1, "http500_path_prefix": "/inquiry"},
                    at_step="s01_click_member_inquiry")]
    else:  # s04 fails; the human gets back to the result list, but does not click the name
        inject = HTTP500_AT_S04
    run = start_replay(app_a, tmp_path, console, injections=inject)
    iv = console.wait_open("replay_failure")
    console.cmd(iv["id"], "claim", operator="carol")
    console.wait_control(iv["id"], "HUMAN")
    h = human(iv["live_view"]["cdp_url"])
    h.nav.click("text=MEMBER INQUIRY")
    h.main.wait_for_selector("input[name=txtA]")
    if scenario == "retry_step":
        h.main.fill("input[name=txtA]", "12345")
        h.main.click("a:has-text('INQUIRE')")
        h.main.wait_for_selector("a[href^='javascript:openMbr']")
    wait_for(lambda: console.get(iv["id"])["human_actions"], 20, "human actions")
    console.cmd(iv["id"], "resume", operator="carol")
    result = run.join()
    assert result.status == "success", result.failure
    assert result.outputs == {"savings_balance": "12345.67"}
    [rec] = [r for r in result.recoveries if r.kind == "human_intervention"]
    assert f"rule {scenario}" in rec.detail
    statuses = {s.step_id: s.status for s in result.steps}
    if scenario == "expect_holds":
        assert statuses["s01_click_member_inquiry"] == "human"
        assert statuses["s02_fill_member_no"] == "ok"
    else:
        assert statuses["s04_click_name"] == "ok"  # retried by automation after the resume
