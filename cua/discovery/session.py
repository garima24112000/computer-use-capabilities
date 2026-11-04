"""Wiring for one discovery run: run dir, redactor, event log, transcript, browser, guarded
surface, recorder and agent. Used by `cua discover` and by the tests (same code path)."""

from pathlib import Path

from cua.artifact import store
from cua.artifact.profile import AppProfile
from cua.discovery.agent import DiscoveryAgent
from cua.discovery.llm import LLMClient
from cua.discovery.models import DiscoveryConfig, DiscoveryGoal, DiscoveryResult
from cua.discovery.recorder import Recorder
from cua.execute import profile_dialog_policy
from cua.hitl.escalation import Escalator
from cua.hitl.hitl import HitlApprover, HitlEscalator, HitlSession
from cua.hitl.interventions import InterventionStore
from cua.obs.events import EventLogger
from cua.obs.evidence import EvidenceCapture
from cua.obs.run import RunContext
from cua.obs.transcript import TranscriptWriter
from cua.policy.guard import Approver, PolicyGuard
from cua.policy.guarded_surface import GuardedSurface
from cua.policy.models import DEFAULT_POLICY_PATH, PolicyConfig, PolicyContext
from cua.policy.redaction import Redactor
from cua.surface.web import PlaywrightSurface
from target_app.background import free_port


def run_discovery(
    goal: DiscoveryGoal,
    llm: LLMClient,
    approver: Approver,
    *,
    config: DiscoveryConfig | None = None,
    escalator: Escalator | None = None,
    headless: bool = True,
    runs_root: Path = Path("runs"),
    capabilities_root: Path = store.CAPABILITIES_DIR,
    profile_name: str = "meridian-core",
    policy_path: Path = DEFAULT_POLICY_PATH,
    trace: bool = False,
    hitl: bool = False,
    console_approval: bool = False,
    hitl_store: InterventionStore | None = None,
    hitl_timeout_s: float = 15 * 60,
    approval_timeout_s: float = 10 * 60,
    argv: list[str] | None = None,
) -> DiscoveryResult:
    goal.contract()  # fail fast (ContractError) before any run dir, browser or LLM call
    policy = PolicyConfig.load(policy_path)
    profile = AppProfile.load_named(profile_name)
    redactor = Redactor(policy.sensitive_labels)
    for inp in goal.inputs:  # sensitive input values never reach logs, prompts or artifacts
        if inp.sensitive:
            redactor.register_sensitive(str(inp.value), "INPUT")
    run = RunContext("discovery", redactor, root=runs_root, capability=goal.capability_id,
                     goal=goal.goal, tenant=goal.tenant, model_id=llm.model_id, trace=trace,
                     argv=argv)
    logger = EventLogger(run, redactor)
    transcript = TranscriptWriter(run, redactor)
    inner = PlaywrightSurface(headless=headless, dialog_policy=profile_dialog_policy(profile),
                              dialog_policy_name=f"profile:{profile.id}",
                              cdp_port=free_port() if hitl else None)
    if hitl or console_approval:
        session = HitlSession(hitl_store or InterventionStore(Path(runs_root) / "_hitl" /
                                                              "interventions.db"),
                              run, logger, redactor, PolicyGuard(policy), headed=not headless)
        if hitl:
            escalator = HitlEscalator(session, hitl_timeout_s)
        if console_approval:
            approver = HitlApprover(session, approval_timeout_s)
    try:
        inner.start()
        run.start_trace(inner)
        ctx = PolicyContext(mode="discovery", run_id=run.run_id)
        surface = GuardedSurface(inner, PolicyGuard(policy), lambda: ctx, logger,
                                 approver=approver, redactor=redactor)
        evidence = EvidenceCapture(surface, run, redactor, policy, logger, actor="agent")
        recorder = Recorder(goal, profile, surface, redactor, logger, run,
                            capabilities_root=capabilities_root)
        agent = DiscoveryAgent(llm, surface, profile, recorder, logger, evidence, transcript,
                               config=config, escalator=escalator)
        return agent.run(goal)
    finally:
        inner.close()
        transcript.close()
        logger.close()


def missing_secrets(profile_name: str = "meridian-core") -> list[str]:
    """Names of profile secrets missing from the environment."""
    return AppProfile.load_named(profile_name).missing_secrets()
