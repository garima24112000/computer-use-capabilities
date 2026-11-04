"""Recoverable and non-recoverable faults, injected via the test harness (POST /__faults)."""

import json

import pytest

from cua.artifact.profile import AppProfile
from cua.hitl.escalation import EscalationDecision
from cua.obs.events import EventLogger
from cua.obs.run import RunContext
from cua.policy.models import PolicyConfig
from cua.policy.redaction import Redactor
from cua.replay.engine import ReplayEngine
from cua.replay.harness import FaultInjection as F
from cua.surface.web import PlaywrightSurface
from tests.replay.conftest import READ, artifact_root, latest, run_dir
from tests.surface.helpers import set_faults

M = {"member_id": "12345"}
KINDS = ["discovered", "fixture"]


def kinds(r) -> list[tuple[str, str]]:
    return [(x.kind, x.id) for x in r.recoveries]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("at", ["s01", "s03"])
def test_interstitial_is_recovered(app_a, replay, kind, at):
    cap = latest(kind, READ)
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
    cap = latest(kind, READ)
    r = replay(app_a, READ, M, kind=kind,
               inject=[F(faults={"expire_session_now": True}, at_step=cap.steps[2].id)])
    assert r.status == "success" and r.outputs == {"savings_balance": "12345.67"}
    assert kinds(r) == [("session_relogin", "session_expired")]


def test_session_expiring_twice_fails_retryable(app_a, replay):
    cap = latest("discovered", READ)
    r = replay(app_a, READ, M, inject=[
        F(faults={"expire_session_now": True}, at_step=cap.steps[2].id),
        F(faults={"expire_session_now": True}, at_step=cap.steps[3].id)])
    assert r.status == "failed"
    assert r.failure.category == "session_expired_repeated" and r.failure.retryable


def test_slow_backend_within_settle_budget_succeeds(app_a, replay):
    r = replay(app_a, READ, M, inject=[F(faults={"latency_ms": 1500, "latency_count": 3})])
    assert r.status == "success" and r.recoveries == []


def _with_settle(tmp_path, kind: str, version: str, *, settle_ms: int, attempts: int,
                 expect_timeout_ms: int | None = None):
    """Copy of ONE artifact version with a tight settle budget (a PATCH-level change).

    Returns (store root, "id@version", the INQUIRE step id, whether that step has an expect)."""
    src = artifact_root(kind, READ) / READ / f"{version}.json"
    if not src.exists():
        pytest.skip(f"{kind} artifact {READ}@{version} not present")
    root = tmp_path / f"settle-{kind}-{version}-{attempts}"
    (root / READ).mkdir(parents=True)
    data = json.loads(src.read_text())
    for s in data["steps"]:
        s["settle_timeout_ms"] = settle_ms
        s["retry"] = {"max_attempts": attempts, "backoff_ms": 0, "on": ["settle_timeout"]}
        if expect_timeout_ms is not None and s.get("expect"):
            s["expect"]["timeout_ms"] = expect_timeout_ms
    (root / READ / f"{version}.json").write_text(json.dumps(data))
    inquire = next(s for s in data["steps"] if s["id"].endswith("_inquire"))
    return root, f"{READ}@{version}", inquire["id"], inquire.get("expect") is not None


# Which settle path each artifact exercises at the INQUIRE step (URL unchanged):
#   discovered 1.0.0: no expect   -> retry policy (settle_retry) / settle_timeout
#   discovered 1.1.0: has expect  -> checkpoint absorbs (slow_settle) / settle_timeout
#   fixture 1.0.0:    has expect  -> same as 1.1.0
ARTIFACTS = [("discovered", "1.0.0", False), ("discovered", "1.1.0", True),
             ("fixture", "1.0.0", True)]


def _latency_at(step_id: str, ms: int) -> list:
    return [F(faults={"latency_ms": ms, "latency_count": 1}, at_step=step_id)]


@pytest.mark.parametrize("kind, version, has_expect", ARTIFACTS)
def test_settle_timeout_is_recovered_never_silently(app_a, replay, tmp_path, kind, version,
                                                    has_expect):
    root, ref, step_id, expect = _with_settle(tmp_path, kind, version, settle_ms=800,
                                              attempts=4)
    assert expect is has_expect  # the artifact really takes the path this case is about
    r = replay(app_a, ref, M, root=root, inject=_latency_at(step_id, 1500))
    assert r.status == "success", r.failure
    assert {x.step_id for x in r.recoveries} == {step_id}
    if has_expect:
        [rec] = r.recoveries
        assert (rec.kind, rec.id) == ("settle_absorbed_by_checkpoint", "slow_settle")
        assert "no settle within 800 ms" in rec.detail and "ms waited" in rec.detail
    else:
        assert {x.kind for x in r.recoveries} == {"settle_retry"}


@pytest.mark.parametrize("kind, version, has_expect", ARTIFACTS)
def test_settle_timeout_fails_retryable_when_nothing_absorbs_it(app_a, replay, tmp_path, kind,
                                                                version, has_expect):
    # No retries; and where there is an expect, its timeout is too short to absorb 2.5 s.
    root, ref, step_id, expect = _with_settle(tmp_path, kind, version, settle_ms=800,
                                              attempts=1, expect_timeout_ms=300)
    assert expect is has_expect
    r = replay(app_a, ref, M, root=root, inject=_latency_at(step_id, 2500))
    assert r.status == "failed"
    assert r.failure.category == "settle_timeout" and r.failure.retryable is True
    assert r.failure.step_id == step_id
    assert r.recoveries == []
    if has_expect:
        assert "then expect failed after" in r.failure.observed


@pytest.mark.parametrize("kind, version, has_expect", [a for a in ARTIFACTS if a[2]])
def test_settle_timeout_with_patient_expect_but_no_retry_is_absorbed(
        app_a, replay, tmp_path, kind, version, has_expect):
    root, ref, step_id, _ = _with_settle(tmp_path, kind, version, settle_ms=800, attempts=1)
    r = replay(app_a, ref, M, root=root, inject=_latency_at(step_id, 2500))
    assert r.status == "success"
    assert [(x.kind, x.step_id) for x in r.recoveries] == [
        ("settle_absorbed_by_checkpoint", step_id)]


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
    cap = latest("discovered", READ)
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
    cap = latest("discovered", READ)
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
    cap = latest("discovered", READ)
    set_faults(app_a, http500_count=1, http500_path_prefix="/member")
    esc = Recorder(EscalationDecision(action="resumed", note="fixed it"))
    engine, logger = _engine(tmp_path, app_a, esc)
    r = engine.run(cap, M, profile=AppProfile.load_named("meridian-core"))
    logger.close()
    # Nobody actually fixed the page: verification fails, the engine escalates once more
    # (round 2), that resume fails too, and the run fails (resume protocol, step 7).
    assert r.status == "failed" and r.failure.category == "app_error"
    assert "after 2 operator rounds" in r.failure.observed
    assert len(esc.requests) == 2
    assert "resume verification failed (round 1)" in esc.requests[1].reason
    assert [x.kind for x in r.recoveries] == ["human_intervention"] * 2
