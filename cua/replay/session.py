"""Wiring for one replay: artifact resolution, run dir, redactor, event log, browser factory.

Used by `cua replay` and by the tests (same code path). No LLM, no cloud credentials.
"""

import json
import re
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from cua.artifact import store
from cua.artifact.overrides import TenantOverride
from cua.artifact.profile import PROFILES_DIR, AppProfile
from cua.hitl.escalation import Escalator
from cua.hitl.hitl import HitlApprover, HitlEscalator, HitlSession
from cua.hitl.interventions import InterventionStore
from cua.obs.events import EventLogger
from cua.obs.run import RunContext
from cua.policy.guard import Approver, PolicyGuard
from cua.policy.models import DEFAULT_POLICY_PATH, PolicyConfig
from cua.policy.redaction import Redactor
from cua.replay.engine import ReplayEngine
from cua.replay.harness import FaultInjection, inject
from cua.replay.result import (
    RETRYABLE,
    CapabilityRef,
    Failure,
    FailureCategory,
    ReplayResult,
)
from cua.surface.base import DialogPolicy
from cua.surface.web import PlaywrightSurface
from target_app.background import free_port

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def resolve_artifact(ref: str, capabilities_root: Path = store.CAPABILITIES_DIR) -> Path:
    """A path, or `id[@version]` (highest semver when no version is given)."""
    path = Path(ref)
    if path.suffix == ".json" and path.exists():
        return path
    cap_id, _, version = ref.partition("@")
    folder = Path(capabilities_root) / cap_id
    if version:
        candidate = folder / f"{version}.json"
        if not candidate.exists():
            raise FileNotFoundError(f"no artifact {candidate}")
        return candidate
    versions = sorted((tuple(map(int, m.groups())), p) for p in folder.glob("*.json")
                      if (m := _SEMVER.match(p.stem)))
    if not versions:
        raise FileNotFoundError(f"no artifacts for {cap_id!r} under {capabilities_root}")
    return versions[-1][1]


def tenant_override_for(tenant: str) -> TenantOverride | None:
    path = PROFILES_DIR / "tenants" / f"tenant-{tenant}.yaml"
    return TenantOverride.load(path) if path.exists() else None


def run_replay(
    artifact: str | Path,
    inputs: dict[str, Any],
    *,
    base_url: str,
    tenant: str = "a",
    approver: Approver | None = None,
    escalator: Escalator | None = None,
    headless: bool = True,
    runs_root: Path = Path("runs"),
    capabilities_root: Path = store.CAPABILITIES_DIR,
    policy_path: Path = DEFAULT_POLICY_PATH,
    injections: list[FaultInjection] | None = None,
    trace: bool = False,
    use_override: bool = True,
    hitl: bool = False,
    console_approval: bool = False,
    hitl_store: "InterventionStore | None" = None,
    hitl_timeout_s: float = 15 * 60,
    approval_timeout_s: float = 10 * 60,
) -> ReplayResult:
    """`hitl`: escalations go to the operator console (HitlEscalator) and the browser gets a
    localhost CDP port for live access. `console_approval`: approvals via the console."""
    policy = PolicyConfig.load(policy_path)
    redactor = Redactor(policy.sensitive_labels)
    path = resolve_artifact(str(artifact), capabilities_root)
    raw = json.loads(path.read_text(encoding="utf-8"))
    run = RunContext("replay", redactor, root=runs_root, capability=raw.get("id"),
                     tenant=tenant, trace=trace)
    logger = EventLogger(run, redactor)
    try:
        try:
            cap, _ = store.load(path)
        except ValidationError as exc:
            return _invalid_artifact(raw, run, logger, str(exc))
        profile = AppProfile.load_named(cap.app.profile)
        injections = injections or []
        for inj in injections:
            if inj.at_step is None:
                inject(base_url, inj, run, logger)
        at_step = {i.at_step: i for i in injections if i.at_step is not None}

        def before_step(step_id: str) -> None:
            if (inj := at_step.pop(step_id, None)) is not None:  # once, even across restarts
                inject(base_url, inj, run, logger)

        if hitl or console_approval:
            session = HitlSession(hitl_store or InterventionStore(Path(runs_root) / "_hitl" /
                                                                  "interventions.db"),
                                  run, logger, redactor, PolicyGuard(policy),
                                  headed=not headless)
            if hitl:
                escalator = HitlEscalator(session, hitl_timeout_s)
            if console_approval:
                approver = HitlApprover(session, approval_timeout_s)

        def factory(dialog_policy: DialogPolicy, name: str) -> PlaywrightSurface:
            surface = PlaywrightSurface(headless=headless, dialog_policy=dialog_policy,
                                        dialog_policy_name=name,
                                        cdp_port=free_port() if hitl else None)
            surface.start()
            run.start_trace(surface)
            return surface

        engine = ReplayEngine(base_url=base_url, policy=policy, run=run, logger=logger,
                              redactor=redactor, surface_factory=factory, approver=approver,
                              escalator=escalator, before_step=before_step)
        return engine.run(cap, inputs, profile=profile,
                          tenant_override=tenant_override_for(tenant) if use_override else None)
    finally:
        logger.close()


def _invalid_artifact(raw: dict, run: RunContext, logger: EventLogger, error: str
                      ) -> ReplayResult:
    from datetime import UTC, datetime

    cat = FailureCategory.artifact_invalid
    result = ReplayResult(
        status="failed", run_id=run.run_id, evidence_dir=str(run.dir),
        started_at=datetime.now(UTC),
        capability=CapabilityRef(id=str(raw.get("id", "?")), version=str(raw.get("version", "?")),
                                 content_hash="", approved=False),
        failure=Failure(category=cat, retryable=RETRYABLE[cat], expected="a valid artifact",
                        observed=error[:2000]))
    logger.emit("failure", "replay", category=cat, observed=result.failure.observed)
    run.write_json("result.json", result.model_dump(mode="json"))
    run.finish("failed")
    return result
