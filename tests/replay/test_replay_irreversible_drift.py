"""Approval gate, irreversible commits, drift (tenant B), evidence and import isolation."""

import ast
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import cua.replay.session as replay_session
from cua.replay.harness import FaultInjection as F
from tests.replay.conftest import OPEN, READ, open_inputs, run_dir

ROOT = Path(__file__).resolve().parents[2]
KINDS = ["discovered", "fixture"]


def accounts_of(base_url: str, member: str) -> str:
    with httpx.Client(base_url=base_url) as http:
        http.post("/login", data={"f1": "demo_operator", "f2": "demo-pass-123"})
        page = http.get(f"/member/detail?m={member}").text
    return page[page.index("ACCOUNTS"):]


@pytest.mark.parametrize("kind", KINDS)
def test_unapproved_irreversible_artifact_is_refused_before_any_browser(
        app_a, replay, monkeypatch, draft_open, kind):
    monkeypatch.setattr(replay_session, "PlaywrightSurface",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser")))
    r = replay(app_a, OPEN, open_inputs("22222"), root=draft_open(kind))
    assert r.status == "failed" and r.failure.category == "artifact_not_approved"
    assert "cua artifact approve" in r.failure.observed
    assert r.capability.approved is False


@pytest.mark.parametrize("kind, member", [("discovered", "31415"), ("fixture", "70001")])
def test_approved_open_commits_and_reports_irreversible(app_a, replay, approved_open, kind,
                                                       member):
    store_ = approved_open(kind)
    before = accounts_of(app_a, member)
    r = replay(app_a, OPEN, open_inputs(member), root=store_.root)
    assert r.status == "success", r.failure
    assert r.capability.approved is True
    assert "MC-" in r.outputs["confirmation_number"]
    assert [s for s in r.irreversible_executed if "submit" in s]
    assert accounts_of(app_a, member) != before


@pytest.mark.parametrize("kind", KINDS)
def test_edited_approved_artifact_is_refused(app_a, replay, approved_open, kind):
    store_ = approved_open(kind)
    data = json.loads(store_.path.read_text())
    data["steps"][5]["settle_timeout_ms"] = 14000  # any functional edit changes the hash
    store_.path.write_text(json.dumps(data))
    r = replay(app_a, OPEN, open_inputs("22222"), root=store_.root)
    assert r.failure.category == "artifact_not_approved"
    assert "hash mismatch" in r.failure.observed


@pytest.mark.parametrize("kind", KINDS)
def test_deposit_limits_follow_the_contract(app_a, replay, approved_open, kind):
    store_ = approved_open(kind)
    deposit = next(p for p in latest_from(store_).inputs if p.name == "initial_deposit")
    ok = replay(app_a, OPEN, open_inputs("22222", deposit="9999.99"), root=store_.root)
    assert ok.status == "success", ok.failure
    big = replay(app_a, OPEN, open_inputs("22222", deposit="20000"), root=store_.root)
    if deposit.max is not None:  # bounded contract: rejected before any browser
        assert big.status == "failed" and big.failure.category == "input_validation"
        assert "must be <= 10000" in big.failure.observed and big.steps == []
    else:  # unbounded contract: the core says no, as a business outcome
        assert big.status == "business_outcome"
        assert big.outcome.code == "DEPOSIT_OUT_OF_RANGE"
    assert big.irreversible_executed == []


@pytest.mark.parametrize("kind", KINDS)
def test_session_lost_after_commit_is_escalatable_not_retried(app_a, replay, approved_open,
                                                              kind):
    store_ = approved_open(kind)
    submit = next(s.id for s in latest_from(store_).steps if s.risk == "irreversible")
    r = replay(app_a, OPEN, open_inputs("55555"), root=store_.root,
               inject=[F(faults={"expire_session_now": True}, at_step=submit)])
    assert r.status == "failed"
    assert r.failure.category == "session_expired_after_commit"
    assert r.failure.retryable is False
    assert r.irreversible_executed == [submit]
    assert not [x for x in r.recoveries if x.kind == "session_relogin"]


def latest_from(store_):
    from cua.artifact import store

    return store.load(store_.path)[0]


@pytest.mark.parametrize("kind", KINDS)
def test_tenant_b_with_override_succeeds(app_b, replay, kind):
    r = replay(app_b, READ, {"member_id": "12345"}, kind=kind, tenant="b")
    assert r.status == "success" and r.outputs == {"savings_balance": "12345.67"}


def test_tenant_b_without_override_never_clicks_on_structure(app_b, replay):
    r = replay(app_b, READ, {"member_id": "12345"}, tenant="b", use_override=False)
    assert r.status == "failed"
    assert r.failure.category == "structural_only_on_risky"
    assert r.failure.step_id == "s03_click_inquire"
    assert {(d.step_id, d.winning_strategy_kind, d.structural_only) for d in r.drift} >= {
        ("s03_click_inquire", "css_path", True)}


def test_fixture_without_structural_fallback_reports_target_not_found(app_b, replay):
    r = replay(app_b, READ, {"member_id": "12345"}, kind="fixture", tenant="b", use_override=False)
    assert r.status == "failed" and r.failure.category == "target_not_found"
    assert r.failure.resolve_attempts and all(
        a["outcome"] != "matched" for a in r.failure.resolve_attempts)


FORBIDDEN = ["demo-pass-123", "900-12-3456", "555-0101", "john.sample@example.com",
             "SAMPLE, JOHN Q", "100 TEST LANE"]


def test_run_dir_evidence_and_no_pii(app_a, replay):
    r = replay(app_a, READ, {"member_id": "12345"},
               inject=[F(faults={"js_confirm_count": 1})])
    assert r.status == "success"
    names = {p.name for p in run_dir(r).iterdir()}
    assert {"result.json", "events.jsonl", "manifest.json", "screenshots"} <= names
    shots = sorted(p.name for p in (run_dir(r) / "screenshots").glob("*.png"))
    assert len(shots) == len(r.steps)
    types = {json.loads(line)["type"] for line in
             (run_dir(r) / "events.jsonl").read_text().splitlines()}
    assert {"replay_started", "step_started", "resolve", "policy_decision", "action",
            "recovery", "sweep_result", "checkpoint", "replay_finished",
            "harness_fault"} <= types
    for path in run_dir(r).rglob("*"):
        if path.is_file():
            blob = path.read_bytes()
            for needle in FORBIDDEN:
                assert needle.encode() not in blob, f"{needle} in {path}"


def test_replay_never_imports_discovery_llm_or_boto3():
    for path in [*(ROOT / "cua" / "replay").rglob("*.py"), ROOT / "cua" / "execute.py",
                 ROOT / "cua" / "hitl" / "escalation.py"]:
        for node in ast.walk(ast.parse(path.read_text())):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else [])
            for m in mods:
                assert not m.startswith(("cua.discovery", "boto3", "botocore")), (path, m)
    code = ("import sys, cua.replay.session, cua.replay.engine; "
            "bad = [m for m in sys.modules if m.startswith(('cua.discovery', 'boto3', "
            "'botocore'))]; print(bad); sys.exit(1 if bad else 0)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
