"""ReplayEngine: runs an approved capability deterministically. No LLM, no cloud credentials.

Phase 0 (no side effects): lint, tenant override, input contract, approval gate.
Phase 1: browser + GuardedSurface, sign-on via the profile's login subflow.
Phase 2: each step = bind → resolve (sweep first if missing) → policy-checked action →
         settle → condition sweep → expect.
Phase 3: success checkpoint → typed outputs.

The condition sweep, in this exact order: dialogs → interstitials (recover, re-sweep) →
session expiry (re-login once, restart from step 1, never after a commit) → hard error
signatures → business outcomes. It runs after every step, and also before declaring a target
missing, because a known state (a notice, an expired session, a not-found message) explains a
missing target better than "drift".

This module must never import cua.discovery, boto3 or any LLM code (a test enforces it).
"""

import re
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from cua.artifact.conditions import Checkpoint, describe_condition
from cua.artifact.contract import OutcomeSpec, OutputField
from cua.artifact.overrides import OverrideError, TenantOverride, apply_override
from cua.artifact.profile import AppProfile
from cua.artifact.schema import Capability
from cua.artifact.steps import Click, Extract, Fill, Navigate, Press, Select, Step
from cua.execute import (
    StepFailed,
    check_checkpoint,
    profile_dialog_policy,
    run_action,
    run_steps,
)
from cua.hitl.escalation import EscalationDecision, EscalationRequest, Escalator, StopEscalator
from cua.obs.events import EventLogger
from cua.obs.evidence import EvidenceCapture
from cua.obs.run import RunContext
from cua.policy.errors import ApprovalDenied, PolicyBlocked, SecretMissing
from cua.policy.guard import Approver, DenyAllApprover, PolicyGuard
from cua.policy.guarded_surface import GuardedSurface
from cua.policy.models import PolicyConfig, PolicyContext
from cua.policy.redaction import Redactor, token
from cua.replay.result import (
    RETRYABLE,
    CapabilityRef,
    Drift,
    Evidence,
    Failure,
    FailureCategory,
    Outcome,
    Recovery,
    ReplayResult,
    StepRecord,
)
from cua.surface.base import DialogPolicy, Surface
from cua.surface.errors import SettleTimeout, SurfaceError, TargetNotFound
from cua.surface.locators import TargetSpec
from cua.templating import render

SurfaceFactory = Callable[[DialogPolicy, str], Surface]  # (dialog policy, its name) -> started


class _Fail(Exception):
    def __init__(self, category: FailureCategory, step_id: str | None, expected: str,
                 observed: str, attempts: list[dict] | None = None):
        self.category, self.step_id = category, step_id
        self.expected, self.observed, self.attempts = expected, observed, attempts or []
        super().__init__(f"{category}: expected {expected}; observed {observed}")


class _BusinessOutcome(Exception):
    def __init__(self, outcome: OutcomeSpec, step_id: str | None):
        self.outcome, self.step_id = outcome, step_id
        super().__init__(outcome.code)


class _Restart(Exception):
    """Session was re-established: run the capability again from step 1."""


def type_output(field: OutputField, value: str | None) -> Any:
    """Normalized text -> typed JSON value; ValueError if it does not fit the type."""
    if value is None:
        return None
    if field.type in ("money", "decimal"):
        try:
            Decimal(value)
        except InvalidOperation:
            raise ValueError(f"{value!r} is not a decimal number") from None
        return value  # decimal string: no float rounding
    if field.type == "integer":
        return int(value)
    if field.type == "date":
        date.fromisoformat(value)
    return value


class ReplayEngine:
    def __init__(self, *, base_url: str, policy: PolicyConfig, run: RunContext,
                 logger: EventLogger, redactor: Redactor, surface_factory: SurfaceFactory,
                 approver: Approver | None = None, escalator: Escalator | None = None,
                 before_step: Callable[[str], None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.base_url = base_url.rstrip("/")
        self.policy = policy
        self.run_ctx = run
        self.log = logger
        self.redactor = redactor
        self.surface_factory = surface_factory
        self.approver = approver or DenyAllApprover()
        self.escalator = escalator or StopEscalator()
        self.before_step = before_step
        self.clock = clock
        self.gs: GuardedSurface | None = None
        self.evidence: EvidenceCapture | None = None

    # --- entry points ---------------------------------------------------------------------------

    def run(self, capability: Capability, inputs: dict[str, Any], *, profile: AppProfile,
            tenant_override: TenantOverride | None = None) -> ReplayResult:
        t0 = self.clock()
        approved = capability.is_approved()  # on the BASE artifact (overrides change the hash)
        self.result = ReplayResult(
            status="failed", run_id=self.run_ctx.run_id, evidence_dir=str(self.run_ctx.dir),
            started_at=datetime.now(UTC),
            capability=CapabilityRef(id=capability.id, version=capability.version,
                                     content_hash=capability.content_hash(), approved=approved))
        self.log.emit("replay_started", "replay", capability=capability.id,
                      version=capability.version, approved=approved,
                      tenant_override=tenant_override.tenant if tenant_override else None)
        try:
            cap, profile = self._phase0(capability, inputs, profile, tenant_override, approved)
            self._execute(cap, profile, approved)
        except _Fail as f:
            self._failed(f, escalate=self.gs is not None)
        except _Handled:
            pass  # a step failure was already reported (and escalated)
        except Exception as exc:  # engine bug: still a typed result with evidence
            self._failed(_Fail(FailureCategory.internal, self._step_id,
                               "replay to run without internal errors",
                               f"{type(exc).__name__}: {exc}"), escalate=self.gs is not None)
        finally:
            if self.gs is not None:
                try:
                    self.gs.close()
                except Exception:
                    pass
        self.result.duration_ms = round((self.clock() - t0) * 1000, 1)
        self.run_ctx.write_json("result.json", self.result.model_dump(mode="json"))
        self.log.emit("replay_finished", "replay", status=self.result.status,
                      category=self.result.failure.category if self.result.failure else None,
                      outcome=self.result.outcome.code if self.result.outcome else None,
                      duration_ms=self.result.duration_ms)
        self.run_ctx.finish(self.result.status)
        return self.result

    # --- phase 0: no side effects ---------------------------------------------------------------

    def _phase0(self, cap: Capability, inputs: dict[str, Any], profile: AppProfile,
                override: TenantOverride | None, approved: bool
                ) -> tuple[Capability, AppProfile]:
        self._step_id: str | None = None
        report = cap.lint(profile.sensitive_labels)
        if not report.ok:
            raise _Fail(FailureCategory.artifact_invalid, None, "a lint-clean artifact",
                        "; ".join(str(e) for e in report.errors))
        if override is not None:
            try:
                cap = apply_override(cap, override)
                if any(a.matches(profile) for a in override.applies_to):
                    profile = apply_override(profile, override)
            except (OverrideError, ValueError) as exc:
                raise _Fail(FailureCategory.artifact_invalid, None,
                            f"tenant override {override.tenant!r} to apply cleanly",
                            str(exc)) from None
        sensitive = {p.name for p in cap.inputs if p.sensitive}
        self.result.inputs = {k: (token("INPUT") if k in sensitive else v)
                              for k, v in inputs.items()}
        errors = cap.validate_inputs(inputs)
        if errors:
            raise _Fail(FailureCategory.input_validation, None,
                        "inputs matching the capability contract",
                        "; ".join(e.message for e in errors))
        for p in cap.inputs:
            if p.sensitive and inputs.get(p.name) not in (None, ""):
                self.redactor.register_sensitive(str(inputs[p.name]), "INPUT")
        self.inputs = {p.name: "" if inputs.get(p.name) is None else str(inputs[p.name])
                       for p in cap.inputs}
        gated = [s.id for s in cap.steps if s.requires_approval or s.risk == "irreversible"]
        if gated and not approved:
            state = (f"status={cap.status}" if cap.status != "approved" else
                     "content changed since approval (hash mismatch)")
            raise _Fail(FailureCategory.artifact_not_approved, None,
                        f"an approved artifact (irreversible steps: {', '.join(gated)})",
                        f"{state}; approve it with `cua artifact approve <path> "
                        "--reviewer NAME` after review")
        return cap, profile

    # --- phases 1-3 -----------------------------------------------------------------------------

    def _ctx(self) -> PolicyContext:
        return PolicyContext(mode="replay", run_id=self.run_ctx.run_id, step_id=self._step_id,
                             artifact_approved=self._approved,
                             step_approved=self._step_approved)

    def _execute(self, cap: Capability, profile: AppProfile, approved: bool) -> None:
        self.cap, self.profile, self._approved, self._step_approved = cap, profile, approved, False
        self._interstitials: dict[str, int] = {}
        self._relogins = 0
        inner = self.surface_factory(profile_dialog_policy(profile), f"profile:{profile.id}")
        self.gs = GuardedSurface(inner, PolicyGuard(self.policy), self._ctx, self.log,
                                 approver=self.approver, redactor=self.redactor)
        self.evidence = EvidenceCapture(self.gs, self.run_ctx, self.redactor, self.policy,
                                        self.log, actor="replay")
        self._step_id = "login"
        self._sign_on()
        try:
            for _ in range(3):  # a restart can only come from a session re-login (bounded)
                try:
                    self._sweep(None)
                    self._steps()
                    self._success()
                    return
                except _Restart:
                    continue
            raise _Fail(FailureCategory.session_expired_repeated, self._step_id,
                        "the run to finish after re-login", "kept restarting")
        except _BusinessOutcome as bo:
            msg = self.redactor.redact_text(render(bo.outcome.message_template, self.inputs))
            self.result.status = "business_outcome"
            self.result.outcome = Outcome(id=bo.outcome.id, code=bo.outcome.code, message=msg)
            self.log.emit("outcome", "replay", bo.step_id, outcome=bo.outcome.id,
                          code=bo.outcome.code, message=msg)

    def _sign_on(self) -> None:
        pre = self.cap.preconditions
        try:
            if pre.session == "login_subflow":
                run_steps(self.gs, self.profile.login_subflow, {}, self.base_url)
            else:
                self.gs.open(self.base_url + pre.entry_path)
        except StepFailed as exc:
            self._sweep_errors("login")
            raise _Fail(FailureCategory.unexpected_state, "login", exc.expected,
                        exc.observed) from None
        except (PolicyBlocked, ApprovalDenied, SecretMissing, SurfaceError) as exc:
            raise self._classify(exc, "login") from None

    def _steps(self) -> None:
        self.outputs: dict[str, Any] = {}
        self.result.steps = [StepRecord(step_id=s.id, status="not_run") for s in self.cap.steps]
        i = 0
        while i < len(self.cap.steps):
            step = self.cap.steps[i]
            try:
                self._run_step(step, i)
            except _Restart:
                self.outputs = {}
                self.result.steps = [StepRecord(step_id=s.id, status="not_run")
                                     for s in self.cap.steps]
                i = 0
                continue
            except _Fail as f:
                self.result.steps[i].status = "failed"
                decision = self._failed(f, escalate=True)
                if decision is None or not decision.resume:
                    raise _Handled() from None
                check = check_checkpoint(self.gs, step.expect, self.inputs) \
                    if step.expect else None
                if check is not None and not check.ok:
                    self._failed(_Fail(FailureCategory.checkpoint_failed, step.id,
                                       step.expect.description,
                                       f"after operator resume: {check.failures()}"),
                                 escalate=False)
                    raise _Handled() from None
                self.result.status = "failed"
                self.result.failure = None
                self.result.steps[i].status = "ok"
            i += 1

    def _run_step(self, step: Step, index: int) -> None:
        self._step_id, self._step_approved = step.id, step.approved
        if self.before_step is not None:
            self.before_step(step.id)
        self.log.emit("step_started", "replay", step.id, intent=step.intent,
                      action=step.action.type, risk=step.risk)
        t0 = self.clock()
        action = step.action
        target: TargetSpec | None = None
        if getattr(action, "target", None) is not None:
            target = action.target.bind(self.inputs)
            self._resolve(step, target)

        if isinstance(action, Extract):
            raw = self._guard(step, lambda: self.gs.read_text(target))
            field = next(o for o in self.cap.outputs if o.name == action.output)
            try:
                value = type_output(field, field.normalize_value(raw))
            except ValueError as exc:
                raise _Fail(FailureCategory.output_invalid, step.id,
                            f"output {field.name} of type {field.type}",
                            self.redactor.redact_text(f"{raw!r}: {exc}")) from None
            if field.sensitive and value is not None:
                self.redactor.register_sensitive(str(value), "OUTPUT")
            self.outputs[field.name] = value
        else:
            self._act(step, target)

        self._sweep(step)
        if step.expect is not None:
            res = check_checkpoint(self.gs, step.expect, self.inputs)
            if not res.ok:
                self._sweep(step)  # slow transition: maybe a known state appeared late
                res = check_checkpoint(self.gs, step.expect, self.inputs)
            self.log.emit("checkpoint", "replay", step.id, ok=res.ok,
                          description=step.expect.description)
            if not res.ok:
                raise _Fail(FailureCategory.checkpoint_failed, step.id,
                            self._render_checkpoint(step.expect), self._observed())
        try:
            self.evidence.capture_step(step.id, step.id, filename=f"{index + 1:02d}-{step.id}.png")
        except SurfaceError:
            pass
        self.result.steps[index] = StepRecord(step_id=step.id, status="ok",
                                              duration_ms=round((self.clock() - t0) * 1000, 1))

    def _resolve(self, step: Step, target: TargetSpec) -> None:
        for _ in range(4):
            try:
                _, rr = self.gs.resolve(target)
                break
            except TargetNotFound as exc:
                attempts = [a.model_dump() for a in exc.attempts]
                self.log.emit("resolve", "replay", step.id, matched=False, attempts=attempts)
                if not self._sweep(step):  # nothing known explains it: real drift
                    raise _Fail(FailureCategory.target_not_found, step.id,
                                f"{target.description!r} in frame {target.frame.key}",
                                self._observed(), attempts) from None
        else:
            raise _Fail(FailureCategory.unexpected_state, step.id, target.description,
                        "target still missing after repeated recoveries")
        win = rr.winning_strategy_index or 0
        kind = target.strategies[win].kind
        structural = kind == "css_path"
        self.log.emit("resolve", "replay", step.id, matched=True, winning_index=win,
                      winning_kind=kind, degraded=rr.degraded,
                      attempts=[a.model_dump() for a in rr.attempts])
        if rr.degraded or structural:
            self.result.drift.append(Drift(step_id=step.id, winning_strategy_kind=kind,
                                           winning_index=win, degraded=rr.degraded,
                                           structural_only=structural))
        if structural and step.risk != "safe":
            raise _Fail(FailureCategory.structural_only_on_risky, step.id,
                        f"a semantic locator for {step.risk} step {target.description!r}",
                        "only the structural css_path matched; refusing to act on structure "
                        "alone", [a.model_dump() for a in rr.attempts])

    def _act(self, step: Step, target: TargetSpec | None) -> None:
        a = step.action
        gs = self.gs
        gs.last_dispatched = None
        if isinstance(a, Navigate):
            self._guard(step, lambda: gs.open(self.base_url + render(a.url_path, self.inputs)))
            return
        if isinstance(a, Click):
            self._guard(step, lambda: gs.click(target, settle=False))
        elif isinstance(a, Fill):
            self._guard(step, lambda: gs.fill(target, render(a.value, self.inputs),
                                              settle=False))
        elif isinstance(a, Select):
            self._guard(step, lambda: gs.select(target, render(a.option, self.inputs),
                                                settle=False))
        elif isinstance(a, Press):
            self._guard(step, lambda: gs.press(a.key, target, settle=False))
        irreversible = step.risk == "irreversible"
        attempts = 1 if irreversible or "settle_timeout" not in step.retry.on \
            else step.retry.max_attempts
        for n in range(1, attempts + 1):
            try:
                gs.wait_for_settle(step.settle_timeout_ms)
                return
            except SettleTimeout as exc:
                if n == attempts:
                    raise _Fail(FailureCategory.settle_timeout, step.id,
                                f"page settled within {step.settle_timeout_ms} ms",
                                str(exc)) from None
                self._recovery("settle_retry", f"wait-{n}", step.id,
                               f"waited again after {exc.reason}")
                time.sleep(step.retry.backoff_ms / 1000)

    def _guard(self, step: Step, fn: Callable[[], Any]) -> Any:
        """Run a surface call, mapping failures to categories; track irreversible dispatches."""
        try:
            return fn()
        except (PolicyBlocked, ApprovalDenied, SecretMissing, SurfaceError) as exc:
            raise self._classify(exc, step.id) from None
        finally:
            d = self.gs.last_dispatched
            if d is not None and (d.risk == "irreversible" or step.risk == "irreversible") \
                    and step.id not in self.result.irreversible_executed:
                self.result.irreversible_executed.append(step.id)

    def _classify(self, exc: Exception, step_id: str | None) -> _Fail:
        msg = self.redactor.redact_text(str(exc))
        if isinstance(exc, PolicyBlocked):
            return _Fail(FailureCategory.policy_blocked, step_id, "action permitted by policy", msg)
        if isinstance(exc, ApprovalDenied):
            return _Fail(FailureCategory.approval_denied, step_id, "approval granted", msg)
        if isinstance(exc, TargetNotFound):
            return _Fail(FailureCategory.target_not_found, step_id, exc.target.description, msg,
                         [a.model_dump() for a in exc.attempts])
        if isinstance(exc, SettleTimeout):
            return _Fail(FailureCategory.settle_timeout, step_id, "page settled", msg)
        return _Fail(FailureCategory.unexpected_state, step_id, "action to succeed", msg)

    def _success(self) -> None:
        self._step_id = "success"
        res = check_checkpoint(self.gs, self.cap.success, self.inputs)
        if not res.ok:
            self._sweep(None)
            res = check_checkpoint(self.gs, self.cap.success, self.inputs)
        self.log.emit("checkpoint", "replay", "success", ok=res.ok,
                      description=self.cap.success.description)
        if not res.ok:
            raise _Fail(FailureCategory.checkpoint_failed, "success",
                        self._render_checkpoint(self.cap.success), self._observed())
        missing = [o.name for o in self.cap.outputs if o.name not in self.outputs]
        if missing:
            raise _Fail(FailureCategory.output_invalid, "success",
                        f"outputs {[o.name for o in self.cap.outputs]}", f"missing {missing}")
        self.result.status = "success"
        # Real values for the caller; sensitive ones are registered with the redactor, so
        # result.json / events on disk carry tokens instead.
        self.result.outputs = dict(self.outputs)

    # --- the condition sweep --------------------------------------------------------------------

    def _recovery(self, kind: str, rid: str, step_id: str | None, detail: str = "") -> None:
        self.result.recoveries.append(Recovery(kind=kind, id=rid, step_id=step_id,
                                               detail=detail))
        self.log.emit("recovery", "replay", step_id, kind=kind, id=rid, detail=detail)

    def _quick(self, cp: Checkpoint) -> bool:
        return check_checkpoint(self.gs, cp, self.inputs, timeout_ms=0).ok

    def _sweep_errors(self, step_id: str | None) -> None:
        for sig in self.profile.error_signatures:
            if sig.hard and self._quick(sig.detect):
                raise _Fail(FailureCategory.app_error, step_id,
                            "no application error", f"{sig.id} ({sig.category}): "
                            f"{sig.description}")

    def _outcomes_for(self, step: Step | None) -> list[OutcomeSpec]:
        specs = list(self.cap.outcomes) + [o for i in self.cap.use_profile_outcomes
                                           if (o := self.profile.outcome(i)) is not None]
        sid = step.id if step else None
        return [o for o in specs if o.check_after_steps == "*" or sid in o.check_after_steps]

    def _sweep(self, step: Step | None) -> bool:
        """Known-state sweep. True if something was recovered; raises for outcomes/failures."""
        sid = step.id if step else self._step_id
        recovered = False
        for _ in range(8):
            obs = self.gs.observe()  # also drains dialogs raised since the last observation
            for d in obs.dialogs:
                known = next((k for k in self.profile.known_dialogs
                              if re.search(k.message_regex, d.message)), None)
                if known is None:
                    raise _Fail(FailureCategory.unknown_dialog, sid, "only known dialogs",
                                self.redactor.redact_text(
                                    f"{d.type} {d.message!r} was {d.action_taken}"))
                self._recovery("dialog", known.id, sid, f"{d.type} {d.action_taken}")
                recovered = True
            hit = None
            for inter in self.profile.interstitials:
                if self._interstitials.get(inter.id, 0) < inter.max_per_run \
                        and self._quick(inter.detect):
                    hit = inter
                    break
            if hit is not None:
                self._interstitials[hit.id] = self._interstitials.get(hit.id, 0) + 1
                for action in hit.recover:
                    self._guard_sweep(lambda a=action: run_action(self.gs, a, self.inputs,
                                                                  self.base_url))
                self._recovery("interstitial", hit.id, sid,
                               f"recovered ({self._interstitials[hit.id]}/{hit.max_per_run})")
                recovered = True
                continue
            if self._quick(self.profile.session_expired.detect):
                if self.result.irreversible_executed:
                    raise _Fail(FailureCategory.session_expired_after_commit, sid,
                                "session alive until the capability finished",
                                "session expired after irreversible step(s) "
                                f"{self.result.irreversible_executed}: a human must verify "
                                "whether the commit happened before any retry")
                if self._relogins >= self.profile.session_expired.max_per_run:
                    raise _Fail(FailureCategory.session_expired_repeated, sid,
                                "one re-login to be enough", "session expired again")
                self._relogins += 1
                self._recovery("session_relogin", "session_expired", sid,
                               "re-ran login subflow; restarting from step 1")
                self._sign_on()
                raise _Restart()
            self._sweep_errors(sid)
            paths = {urlsplit(u).path for u in self.gs.current_urls().values()}
            for o in self._outcomes_for(step):
                if o.in_scope(paths) and self._quick(o.detect):
                    raise _BusinessOutcome(o, sid)
            break
        self.log.emit("sweep_result", "replay", sid, recovered=recovered)
        return recovered

    def _guard_sweep(self, fn: Callable[[], Any]) -> None:
        try:
            fn()
        except (PolicyBlocked, ApprovalDenied, SurfaceError) as exc:
            raise self._classify(exc, self._step_id) from None

    # --- reporting ------------------------------------------------------------------------------

    def _render_checkpoint(self, cp: Checkpoint) -> str:
        parts = [render(describe_condition(c), self.inputs) for _, c in cp.conditions()]
        return self.redactor.redact_text(f"{cp.description}: " + "; ".join(parts))

    def _observed(self) -> str:
        try:
            urls = self.gs.current_urls()
            obs = self.gs.observe()
        except Exception as exc:
            return f"(page unavailable: {type(exc).__name__})"
        paths = {k: urlsplit(u).path for k, u in urls.items()}
        titles = {f.frame.key: f.title for f in obs.frames}
        main = obs.text_by_frame.get("main", "")[:300]
        return self.redactor.redact_text(f"paths {paths}; titles {titles}; main text {main!r}")

    def _failed(self, f: _Fail, escalate: bool) -> EscalationDecision | None:
        failure = Failure(category=f.category, step_id=f.step_id,
                          retryable=RETRYABLE[f.category],
                          expected=self.redactor.redact_text(f.expected),
                          observed=self.redactor.redact_text(f.observed),
                          resolve_attempts=f.attempts)
        self.result.status = "failed"
        self.result.failure = failure
        self.log.emit("failure", "replay", f.step_id, category=f.category,
                      retryable=failure.retryable, expected=failure.expected,
                      observed=failure.observed)
        if not escalate or self.evidence is None:
            return None
        try:
            refs = self.evidence.capture_failure(str(f.category), f.step_id)
            failure.evidence = Evidence(**refs)
        except Exception:
            pass
        decision = self.escalator.escalate(EscalationRequest(
            reason=f"{f.category}: {failure.expected} / {failure.observed}", raised_by="replay",
            run_id=self.run_ctx.run_id, step_id=f.step_id, category=str(f.category),
            evidence=failure.evidence.model_dump(exclude_none=True)))
        failure.escalation = decision.model_dump()
        self.log.emit("escalation", "replay", f.step_id, action=decision.action,
                      note=decision.note, resolution=decision.resolution)
        if decision.action == "handled":
            self.result.status = "escalated"
        return decision


class _Handled(Exception):
    """A step failure was reported (and possibly escalated); stop the run."""
