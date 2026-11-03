"""The discovery loop: observe → decide (LLM) → validate → act (GuardedSurface) → record.

The model only ever sees redacted observations and redacted prompts. It acts only through tools
that are validated against the current observation, and it never sees credentials, because
sign-on runs deterministically from the app profile before the loop. Interstitials, known
dialogs and session expiry are handled deterministically too. The model is never consulted for
them.
"""

import hashlib
import time
from collections.abc import Callable
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from cua.artifact.contract import OutputField
from cua.artifact.profile import AppProfile
from cua.discovery.llm import (
    LLMClient,
    Message,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from cua.discovery.models import (
    DiscoveryConfig,
    DiscoveryGoal,
    DiscoveryResult,
    ExecutedAction,
    Status,
)
from cua.discovery.prompts import COMPACTED, PROMPT_VERSION, system_prompt, turn_message
from cua.discovery.recorder import Recorder
from cua.discovery.tools import TOOLS, ToolValidationError, ValidatedCall, validate_call
from cua.execute import StepFailed, check_checkpoint, run_action, run_steps
from cua.obs.events import EventLogger
from cua.obs.evidence import EvidenceCapture
from cua.obs.transcript import TranscriptWriter
from cua.policy.errors import ApprovalDenied, PolicyBlocked
from cua.policy.guarded_surface import GuardedSurface
from cua.policy.models import PolicyContext, PolicyDecision
from cua.surface.errors import SurfaceError
from cua.surface.models import Observation
from cua.templating import render

# --- escalation seam (step 7 plugs the HITL operator in here) -------------------------------------


class EscalationRequest(BaseModel):
    reason: str
    raised_by: Literal["agent", "system"]
    run_id: str
    turn: int
    evidence: dict[str, str] = {}


class EscalationDecision(BaseModel):
    resume: bool = False
    note: str | None = None  # passed to the model as the "last action result" when resuming


class Escalator(Protocol):
    def escalate(self, request: EscalationRequest) -> EscalationDecision: ...


class StopEscalator:
    """Default: the escalation is logged with evidence by the agent; the run stops."""

    def escalate(self, request: EscalationRequest) -> EscalationDecision:
        return EscalationDecision(resume=False)


class _Stop(Exception):
    def __init__(self, status: Status, reason: str | None = None, outcome: str | None = None,
                 raised_by: Literal["agent", "system"] = "system"):
        self.status, self.reason, self.outcome, self.raised_by = status, reason, outcome, raised_by
        self.handled = False  # escalation already logged + sent to the Escalator
        super().__init__(reason or status)


class _State(BaseModel):
    """Mutable loop state (kept together so an escalation can resume the loop)."""

    model_config = {"arbitrary_types_allowed": True}

    obs: Observation
    history: list["_Turn"] = Field(default_factory=list)
    pending: list[ToolResultBlock] = Field(default_factory=list)
    last_result: str = "signed on; console loaded"
    invalid: int = 0
    no_progress: int = 0
    seen_values: set[tuple[str, str, str]] = Field(default_factory=set)


class _Turn(BaseModel):
    user_text: str
    assistant: list[Any]  # ToolUseBlock | TextBlock
    results: list[ToolResultBlock] = []


_State.model_rebuild()


def fingerprint(obs: Observation) -> str:
    """Page identity for progress detection: frame URLs + titles + element roles/names."""
    parts = sorted(f"{f.frame.key}|{f.url}|{f.title}" for f in obs.frames)
    parts += [f"{e.frame.key}|{e.role}|{e.name}" for e in obs.elements]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


class DiscoveryAgent:
    def __init__(self, llm: LLMClient, surface: GuardedSurface, profile: AppProfile,
                 recorder: Recorder, logger: EventLogger, evidence: EvidenceCapture,
                 transcript: TranscriptWriter, config: DiscoveryConfig | None = None,
                 escalator: Escalator | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.llm = llm
        self.surface = surface
        self.profile = profile
        self.recorder = recorder
        self.log = logger
        self.evidence = evidence
        self.transcript = transcript
        self.config = config or DiscoveryConfig()
        self.escalator = escalator or StopEscalator()
        self.clock = clock
        self.run_ctx = recorder.run
        self.redactor = surface.redactor
        self._ctx = PolicyContext(mode="discovery", run_id=self.run_ctx.run_id)
        surface.ctx_provider = lambda: self._ctx  # the agent owns the policy context
        self._recoveries: dict[str, int] = {}
        self.usage = Usage()

    # --- deterministic parts --------------------------------------------------------------------

    def _sign_on(self, base_url: str) -> None:
        self._ctx = self._ctx.model_copy(update={"step_id": "login"})
        try:
            run_steps(self.surface, self.profile.login_subflow, {}, base_url)
        except StepFailed as exc:
            raise _Stop("escalated", f"sign-on failed: {exc}") from exc
        top = self.surface.current_urls().get("_top", "")
        if urlsplit(top).path != "/":
            self.surface.open(base_url.rstrip("/") + "/")  # post-login landing page (frameset)

    def _recover(self, base_url: str) -> bool:
        """Handle interstitials / session expiry / hard errors. True if anything ran."""
        ran = False
        for _ in range(5):
            hit = False
            for inter in self.profile.interstitials:
                if self._recoveries.get(inter.id, 0) >= inter.max_per_run:
                    continue
                if check_checkpoint(self.surface, inter.detect, {}, timeout_ms=0).ok:
                    self._recoveries[inter.id] = self._recoveries.get(inter.id, 0) + 1
                    for action in inter.recover:
                        run_action(self.surface, action, {}, base_url)
                    self.log.emit("recovery", "system", self._ctx.step_id, kind="interstitial",
                                  id=inter.id, count=self._recoveries[inter.id])
                    hit = ran = True
            exp = self.profile.session_expired
            if self._recoveries.get("session", 0) < exp.max_per_run and check_checkpoint(
                    self.surface, exp.detect, {}, timeout_ms=0).ok:
                self._recoveries["session"] = self._recoveries.get("session", 0) + 1
                self.log.emit("recovery", "system", self._ctx.step_id, kind="session_expired")
                self._sign_on(base_url)
                hit = ran = True
            if not hit:
                break
        for sig in self.profile.error_signatures:
            if sig.hard and check_checkpoint(self.surface, sig.detect, {}, timeout_ms=0).ok:
                raise _Stop("escalated", f"{sig.category}: {sig.description} ({sig.id})")
        return ran

    def _detect_outcome(self) -> str | None:
        for o in self.profile.outcomes:
            if check_checkpoint(self.surface, o.detect, {}, timeout_ms=0).ok:
                return o.id
        return None

    # --- model I/O ------------------------------------------------------------------------------

    def _messages(self, history: list[_Turn], current_text: str, turn: int,
                  pending_results: list[ToolResultBlock]) -> list[Message]:
        msgs: list[Message] = []
        prev: list[ToolResultBlock] = []
        for i, t in enumerate(history, 1):
            msgs.append(Message(role="user", content=[*prev, TextBlock(
                text=COMPACTED.format(turn=i))]))
            msgs.append(Message(role="assistant", content=t.assistant))
            prev = t.results
        msgs.append(Message(role="user",
                            content=[*pending_results, TextBlock(text=current_text)]))
        return msgs

    # --- main loop ------------------------------------------------------------------------------

    def run(self, goal: DiscoveryGoal) -> DiscoveryResult:
        started = self.clock()
        result = DiscoveryResult(status="error", run_id=self.run_ctx.run_id,
                                 run_dir=str(self.run_ctx.dir))
        self.log.emit("run_started", "system", goal=goal.goal, capability=goal.capability_id,
                      model=self.llm.model_id, prompt_version=PROMPT_VERSION,
                      tenant=goal.tenant)
        try:
            self._loop(goal, result, started)
        except _Stop as stop:
            result.status, result.reason, result.outcome = stop.status, stop.reason, stop.outcome
            if stop.status == "escalated" and not stop.handled:
                self._escalate(stop, result)
        except Exception as exc:  # unexpected: record evidence, never crash silently
            result.status, result.reason = "error", f"{type(exc).__name__}: {exc}"
            self.log.emit("error", "system", self._ctx.step_id, error=result.reason)
            self._capture_failure("unexpected-error")
        # Everything after the loop runs AFTER possible side effects in the app: whatever
        # fails here, the evidence (summary.json, error event, snapshot) must still be written.
        try:
            result.recorded_steps = len(self.recorder.steps) \
                if result.status == "completed" else 0
            if result.status == "completed":
                self.transcript.close()
                path, rejected, errors, _ = self.recorder.build(self.llm.model_id,
                                                                self.transcript.sha256())
                result.artifact_path = str(path) if path else None
                result.rejected_path = str(rejected) if rejected else None
                result.lint_errors = errors
        except Exception as exc:
            self._fail(result, exc, "artifact build")
        result.usage = self.usage
        result.wall_s = round(self.clock() - started, 2)
        try:
            self._finish(result)
        except Exception as exc:  # last resort: a minimal summary, never a lost run
            self._fail(result, exc, "finishing the run")
            self.run_ctx.write_json("summary.json", result.model_dump(
                mode="json", exclude={"outputs"}))
        return result

    def _fail(self, result: DiscoveryResult, exc: Exception, phase: str) -> None:
        result.status = "error"
        result.reason = f"{type(exc).__name__} during {phase}: {exc}"
        try:
            self.log.emit("error", "system", self._ctx.step_id, phase=phase,
                          error_type=type(exc).__name__, error=str(exc),
                          irreversible_executed=result.irreversible_executed,
                          irreversible_steps_executed=result.irreversible_steps_executed)
        except Exception:
            pass
        self._capture_failure(f"error-{phase}")

    def _executed(self, result: DiscoveryResult, desc: str, decision: PolicyDecision,
                  ok: bool = True) -> None:
        turn = self._ctx.step_id or "?"
        result.executed_actions.append(ExecutedAction(
            turn=turn, action=self.redactor.redact_text(desc), risk=decision.risk,
            verdict=decision.verdict, rule_id=decision.rule_id, ok=ok))
        if decision.risk == "irreversible":
            result.irreversible_executed = True
            label = f"{turn} {self.redactor.redact_text(desc)}"
            result.irreversible_steps_executed.append(label + ("" if ok else " (failed after "
                                                                               "dispatch)"))

    def _capture_failure(self, reason: str) -> dict[str, str]:
        try:
            return self.evidence.capture_failure(reason, self._ctx.step_id)
        except Exception:  # evidence must never mask the real failure
            return {}

    def _escalate(self, stop: _Stop, result: DiscoveryResult) -> EscalationDecision:
        stop.handled = True
        refs = self._capture_failure("escalation")
        self.log.emit("escalation", stop.raised_by, self._ctx.step_id, reason=stop.reason,
                      evidence=refs)
        decision = self.escalator.escalate(EscalationRequest(
            reason=stop.reason or "escalated", raised_by=stop.raised_by,
            run_id=self.run_ctx.run_id, turn=result.turns, evidence=refs))
        self.log.emit("human_action" if decision.resume else "escalation", "system",
                      self._ctx.step_id, resume=decision.resume, note=decision.note)
        return decision

    def _finish(self, result: DiscoveryResult) -> None:
        sensitive_outputs = {o.name for o in self.recorder.goal.outputs if o.sensitive}
        summary = result.model_dump(mode="json")
        summary["outputs"] = {k: ("[REDACTED:OUTPUT]" if k in sensitive_outputs else v)
                              for k, v in result.outputs.items()}
        self.run_ctx.write_json("summary.json", summary)
        self.log.emit("run_finished", "system", status=result.status, reason=result.reason,
                      outcome=result.outcome, artifact=result.artifact_path,
                      rejected=result.rejected_path, turns=result.turns,
                      usage=self.usage, wall_s=result.wall_s)
        self.transcript.close()
        self.run_ctx.finish(result.status)

    def _loop(self, goal: DiscoveryGoal, result: DiscoveryResult, started: float) -> None:
        system = self.redactor.redact_text(system_prompt(goal))
        self.transcript.write("system", prompt_version=PROMPT_VERSION, system=system,
                              tools=[t.model_dump() for t in TOOLS])
        self._sign_on(goal.base_url)
        self._recover(goal.base_url)
        st = _State(obs=self.surface.observe())
        self.recorder.observe_pages(st.obs)
        while True:
            try:
                if self._turn(goal, result, started, system, st):
                    return
            except _Stop as stop:
                if stop.status != "escalated":
                    raise
                decision = self._escalate(stop, result)
                if not decision.resume:
                    raise
                # An operator fixed things (step 7): reset the budgets and look again.
                st.invalid = st.no_progress = 0
                st.last_result = f"operator: {decision.note or 'resumed after escalation'}"
                st.obs = self.surface.observe()

    def _turn(self, goal: DiscoveryGoal, result: DiscoveryResult, started: float,
              system: str, st: _State) -> bool:
        """One observe/decide/act turn. Returns True when the run completed."""
        cfg = self.config
        inputs = goal.input_values()
        declared_outputs = [o.name for o in goal.outputs]
        if self.clock() - started > cfg.wall_timeout_s:
            raise _Stop("escalated", f"wall-clock timeout ({cfg.wall_timeout_s:.0f}s)")
        if result.turns >= cfg.max_steps:
            raise _Stop("escalated", f"max_steps reached ({cfg.max_steps})")
        result.turns += 1
        turn = result.turns
        self._ctx = self._ctx.model_copy(update={"step_id": f"t{turn:02d}"})
        text = self.redactor.redact_text(turn_message(
            turn, cfg.max_steps, st.last_result,
            [o for o in declared_outputs if o in result.outputs],
            [o for o in declared_outputs if o not in result.outputs],
            st.obs.to_prompt_text()))
        messages = self._messages(st.history, text, turn, st.pending)
        self.transcript.write("request", turn=turn, message=messages[-1].model_dump())
        response = self.llm.decide(system, messages, TOOLS)
        self.usage = Usage(input_tokens=self.usage.input_tokens + response.usage.input_tokens,
                           output_tokens=self.usage.output_tokens + response.usage.output_tokens)
        self.transcript.write("response", turn=turn, response=response.model_dump())
        calls = response.tool_calls
        first: ToolCall | None = calls[0] if calls else None
        self.log.emit("llm_decision", "agent", self._ctx.step_id,
                      tool=first.name if first else None,
                      intent=(first.args or {}).get("intent") if first else None,
                      ref=(first.args or {}).get("ref") if first else None,
                      usage=response.usage, stop_reason=response.raw_stop_reason)
        assistant: list[Any] = [ToolUseBlock(id=c.id, name=c.name, args=c.args)
                                for c in calls] or [TextBlock(text=response.text or "(none)")]
        turn_rec = _Turn(user_text=text, assistant=assistant)
        st.history.append(turn_rec)
        extra = [ToolResultBlock(tool_use_id=c.id, content="ignored: one action per turn",
                                 is_error=True) for c in calls[1:]]

        def reply(content: str) -> None:
            content = self.redactor.redact_text(content)  # tool results go to the model too
            st.last_result = content
            head = [ToolResultBlock(tool_use_id=first.id, content=content,
                                    is_error=content.startswith("error"))] if first else []
            turn_rec.results = st.pending = head + extra

        try:
            if first is None:
                raise ToolValidationError("no tool call; call exactly one tool per turn")
            call = validate_call(first, st.obs, set(inputs), set(declared_outputs))
        except ToolValidationError as exc:
            st.invalid += 1
            reply(f"error: invalid tool call: {exc}")
            if st.invalid >= cfg.max_invalid_calls:
                raise _Stop("escalated", f"{st.invalid} consecutive invalid tool calls") from None
            return False
        st.invalid = 0

        if call.name == "escalate":
            raise _Stop("escalated", self.redactor.redact_text(call.text), raised_by="agent")
        if call.name == "done":
            missing = [o for o in declared_outputs if o not in result.outputs]
            if not missing:
                self.recorder.finalize_success()
                result.status = "completed"
                return True
            outcome = self._detect_outcome()
            if outcome is not None:
                code = self.profile.outcome(outcome).code  # type: ignore[union-attr]
                self.log.emit("outcome", "agent", self._ctx.step_id, outcome=outcome,
                              code=code, summary=call.text)
                raise _Stop("business_outcome", call.text, outcome=code)
            st.invalid += 1
            reply(f"error: cannot finish: outputs not extracted: {missing} and no business "
                  "outcome is shown")
            if st.invalid >= cfg.max_invalid_calls:
                raise _Stop("escalated", "done called repeatedly without outputs")
            return False

        summary, st.obs, progressed = self._act(call, st.obs, goal, inputs, result,
                                                st.seen_values)
        reply(summary)
        st.no_progress = 0 if progressed else st.no_progress + 1
        if st.no_progress >= cfg.no_progress_limit:
            raise _Stop("escalated", f"no progress: page unchanged after {st.no_progress} "
                                     "consecutive actions")
        return False

    def _act(self, call: ValidatedCall, pre: Observation, goal: DiscoveryGoal,
             inputs: dict[str, str], result: DiscoveryResult,
             seen_values: set[tuple[str, str, str]]) -> tuple[str, Observation, bool]:
        el = next(e for e in pre.elements if e.ref == call.ref) if call.ref else None
        pre_fp = fingerprint(pre)
        desc = f'{call.name} {el.role} "{el.name}"' if el else call.name
        raw_text: str | None = None
        try:
            if call.name == "extract":
                raw_text = self.surface.read_text(call.ref)  # redacted per sensitivity
            target = self.recorder.target_for(call.ref, el, raw_text) if el else None
        except SurfaceError as exc:
            return f"error: {desc}: cannot record a durable locator ({exc})", pre, False

        if call.name == "extract":
            out = next(o for o in goal.outputs if o.name == call.output)
            field = OutputField(name=out.name, type=out.type, description=out.name,
                                normalize=out.normalize, extract_pattern=out.extract_pattern)
            try:
                value = field.normalize_value(raw_text)
            except ValueError as exc:
                return f"error: {desc}: {exc}", pre, False
            if out.sensitive and value:
                self.redactor.register_sensitive(value, "OUTPUT")
            result.outputs[out.name] = value
            self.recorder.record_extract(call, target, el)
            result.actions_executed += 1
            self.log.emit("action", "agent", self._ctx.step_id, action=f"extract {call.output}",
                          ok=True, value=value)
            key = ("extract", call.output or "", value or "")
            novel = key not in seen_values
            seen_values.add(key)
            return f"ok: extracted {call.output} = {value}", pre, novel

        self.surface.last_dispatched = None
        try:
            if call.name == "click":
                res = self.surface.click(call.ref)
            elif call.name == "fill":
                res = self.surface.fill(call.ref, render(call.value or "", inputs))
            elif call.name == "select":
                res = self.surface.select(call.ref, render(call.value or "", inputs))
            else:
                res = self.surface.press(call.key or "Enter", call.ref)
        except (ApprovalDenied, PolicyBlocked) as exc:
            raise _Stop("escalated", f"{type(exc).__name__}: {exc}") from exc
        except SurfaceError as exc:
            if self.surface.last_dispatched is not None:  # authorized + dispatched, then failed
                self._executed(result, desc, self.surface.last_dispatched, ok=False)
            return f"error: {desc}: {exc}", pre, False
        result.actions_executed += 1
        self._executed(result, desc, res.policy)
        self._recover(goal.base_url)
        if self.config.screenshots:
            self.evidence.capture_step(desc, self._ctx.step_id,
                                       filename=f"step{result.turns:02d}.png")
        post = self.surface.observe()
        self.recorder.observe_pages(post)
        changed = fingerprint(post) != pre_fp
        key = (call.name, el.name if el else "", call.value or "")
        novel = call.name in ("fill", "select") and key not in seen_values
        seen_values.add(key)
        self.recorder.record_action(call, target, el, res.policy, pre, post, changed)
        dialogs = ", ".join(f"{d.type} '{d.message}' {d.action_taken}" for d in res.dialogs)
        summary = (f"ok: {desc}; policy {res.policy.risk}/{res.policy.verdict}; "
                   f"navigated={res.navigated}; page changed={'yes' if changed else 'no'}"
                   + (f"; dialogs: {dialogs}" if dialogs else ""))
        return summary, post, changed or novel
