"""Recoverable and non-recoverable faults, injected via the test harness (POST /__faults)."""

import json
import shutil

import pytest

from cua.artifact import store
from cua.artifact.profile import AppProfile
from cua.hitl.escalation import EscalationDecision
from cua.obs.events import EventLogger
from cua.obs.run import RunContext
from cua.policy.models import PolicyConfig
from cua.policy.redaction import Redactor
from cua.replay.engine import ReplayEngine
from cua.replay.harness import FaultInjection as F
from cua.surface.web import PlaywrightSurface
from tests.replay.conftest import READ, artifact_root, run_dir
from tests.surface.helpers import set_faults

M = {"member_id": "12345"}
KINDS = ["discovered", "fixture"]


def kinds(r) -> list[tuple[str, str]]:
    return [(x.kind, x.id) for x in r.recoveries]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("at", ["s01", "s03"])
def test_interstitial_is_recovered(app_a, replay, kind, at):
    cap, _ = store.load(next((artifact_root(kind, READ) / READ).glob("1.0.0.json")))
    step = next(s.id for s in cap.steps if s.id.startswith(at))
    r = replay(app_a, READ, M, kind=kind, inject=[F(faults={"interstitial_count": 1},
                                                    at_step=step)])
    assert r.status == "success" and r.outputs == {"savings_balance": "12345.67"}
    assert kinds(r) == [("interstitial", "maintenance_notice")]
    assert r.recoveries[0].step_id == step
    manifest = json.loads((run_dir(r) / "manifest.json").read_text())
    assert manifest["harness_faults"] == [{"faults": {"interstitial_count": 1},
                                           "at_step": step}]


@pytest.mark.parametrize("kind", KINDS)
def test_known_dialog_is_accepted(app_a, replay, kind):
    r = replay(app_a, READ, M, kind=kind, inject=[F(faults={"js_confirm_count": 1})])
    assert r.status == "success" and kinds(r) == [("dialog", "unposted_items")]


@pytest.mark.parametrize("kind", KINDS)
def test_session_expiry_relogs_in_and_restarts(app_a, replay, kind):
    cap, _ = store.load(artifact_root(kind, READ) / READ / "1.0.0.json")
    r = replay(app_a, READ, M, kind=kind,
               inject=[F(faults={"expire_session_now": True}, at_step=cap.steps[2].id)])
    assert r.status == "success" and r.outputs == {"savings_balance": "12345.67"}
    assert kinds(r) == [("session_relogin", "session_expired")]


def test_session_expiring_twice_fails_retryable(app_a, replay):
    cap, _ = store.load(artifact_root("discovered", READ) / READ / "1.0.0.json")
    r = replay(app_a, READ, M, inject=[
        F(faults={"expire_session_now": True}, at_step=cap.steps[2].id),
        F(faults={"expire_session_now": True}, at_step=cap.steps[3].id)])
    assert r.status == "failed"
    assert r.failure.category == "session_expired_repeated" and r.failure.retryable


def test_slow_backend_within_settle_budget_succeeds(app_a, replay):
    r = replay(app_a, READ, M, inject=[F(faults={"latency_ms": 1500, "latency_count": 3})])
    assert r.status == "success" and r.recoveries == []


def _with_settle(tmp_path, ms: int, attempts: int):
    """Copy of the discovered artifact with a tight settle budget (a PATCH-level change)."""
    root = tmp_path / f"settle-{attempts}"
    shutil.copytree(artifact_root("discovered", READ), root, dirs_exist_ok=True)
    path = root / READ / "1.0.0.json"
    data = json.loads(path.read_text())
    for s in data["steps"]:
        s["settle_timeout_ms"] = ms
        s["retry"] = {"max_attempts": attempts, "backoff_ms": 0, "on": ["settle_timeout"]}
    path.write_text(json.dumps(data))
    return root


def test_settle_timeout_is_retried_per_step_policy(app_a, replay, tmp_path):
    r = replay(app_a, READ, M, root=_with_settle(tmp_path, 800, 4),
               inject=[F(faults={"latency_ms": 1500, "latency_count": 1},
                         at_step="s03_click_inquire")])
    assert r.status == "success"
    assert {x.kind for x in r.recoveries} == {"settle_retry"}
    assert {x.step_id for x in r.recoveries} == {"s03_click_inquire"}


def test_settle_timeout_without_retry_fails_retryable(app_a, replay, tmp_path):
    r = replay(app_a, READ, M, root=_with_settle(tmp_path, 800, 1),
               inject=[F(faults={"latency_ms": 2500, "latency_count": 1},
                         at_step="s03_click_inquire")])
    assert r.status == "failed"
    assert r.failure.category == "settle_timeout" and r.failure.retryable is True
    assert r.failure.step_id == "s03_click_inquire"


@pytest.mark.parametrize("kind", KINDS)
def test_system_error_is_app_error_retryable_with_evidence(app_a, replay, kind):
    r = replay(app_a, READ, M, kind=kind,
               inject=[F(faults={"http500_count": 1, "http500_path_prefix": "/member"})])
    assert r.status == "failed"
    f = r.failure
    assert f.category == "app_error" and f.retryable is True
    assert "system_error_coffee" in f.observed
    assert (run_dir(r) / f.evidence.screenshot).exists()
    assert "SYSTEM ERROR" in (run_dir(r) / f.evidence.snapshot).read_text()


# --- engine-level tests (custom profile / escalator) ------------------------------------------


def _engine(tmp_path, base_url, escalator=None):
    policy = PolicyConfig.load()
    redactor = Redactor(policy.sensitive_labels)
    run = RunContext("replay", redactor, root=tmp_path / "runs")
    logger = EventLogger(run, redactor)

    def factory(dialog_policy, name):
        s = PlaywrightSurface(headless=True, dialog_policy=dialog_policy,
                              dialog_policy_name=name)
        s.start()
        return s

    return ReplayEngine(base_url=base_url, policy=policy, run=run, logger=logger,
                        redactor=redactor, surface_factory=factory, escalator=escalator), logger


def test_unknown_dialog_fails(app_a, tmp_path):
    profile = AppProfile.load_named("meridian-core").model_copy(update={"known_dialogs": []})
    cap, _ = store.load(artifact_root("discovered", READ) / READ / "1.0.0.json")
    set_faults(app_a, js_confirm_count=1)
    engine, logger = _engine(tmp_path, app_a)
    r = engine.run(cap, M, profile=profile)
    logger.close()
    assert r.status == "failed" and r.failure.category == "unknown_dialog"
    assert "Unposted items exist" in r.failure.observed


class Recorder:
    def __init__(self, decision):
        self.decision, self.requests = decision, []

    def escalate(self, request):
        self.requests.append(request)
        return self.decision


def test_escalator_handled_marks_run_escalated(app_a, tmp_path):
    cap, _ = store.load(artifact_root("discovered", READ) / READ / "1.0.0.json")
    set_faults(app_a, http500_count=1, http500_path_prefix="/member")
    esc = Recorder(EscalationDecision(action="handled", resolution="operator read it by phone"))
    engine, logger = _engine(tmp_path, app_a, esc)
    r = engine.run(cap, M, profile=AppProfile.load_named("meridian-core"))
    logger.close()
    assert r.status == "escalated"
    assert r.failure.escalation["resolution"] == "operator read it by phone"
    [req] = esc.requests
    assert (req.category, req.raised_by) == ("app_error", "replay")
    assert req.evidence["screenshot"]


def test_escalator_resume_reverifies_the_step(app_a, tmp_path):
    cap, _ = store.load(artifact_root("discovered", READ) / READ / "1.0.0.json")
    set_faults(app_a, http500_count=1, http500_path_prefix="/member")
    esc = Recorder(EscalationDecision(action="resumed", note="fixed it"))
    engine, logger = _engine(tmp_path, app_a, esc)
    r = engine.run(cap, M, profile=AppProfile.load_named("meridian-core"))
    logger.close()
    # Nobody actually fixed the page, so re-verification of the step's expect fails.
    assert r.status == "failed" and r.failure.category == "checkpoint_failed"
    assert "after operator resume" in r.failure.observed
