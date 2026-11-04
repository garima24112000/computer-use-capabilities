"""HITL implementations of the escalation and approval seams, and the per-run session.

HitlSession is the glue for one run:
- the ControlSession (state + lease), mirrored into the InterventionStore for the console,
  logged as control_transition events and shown as an in-page banner;
- HumanCapture (only active while a human holds control);
- live.png: a masked screenshot written every ~2 s while a human is in control.

The run thread blocks inside escalate()/request_approval() while it waits for the console. It
waits with page.wait_for_timeout(), never time.sleep, so Playwright keeps dispatching capture
callbacks and navigation events from the live browser.
"""

import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from cua.hitl.capture import HumanCapture
from cua.hitl.control import BANNER_JS, ControlSession, ControlState, Transition, banner_text
from cua.hitl.escalation import EscalationDecision, EscalationRequest
from cua.hitl.interventions import (
    HumanAction,
    InterventionRequest,
    InterventionStore,
    LiveView,
)
from cua.obs.events import EventLogger
from cua.obs.run import RunContext
from cua.policy.guard import PolicyGuard
from cua.policy.models import PolicyContext, PolicyDecision
from cua.policy.redaction import Redactor

LIVE_EVERY_S = 2.0


class HitlSession:
    def __init__(self, store: InterventionStore, run: RunContext, logger: EventLogger,
                 redactor: Redactor, guard: PolicyGuard, *, headed: bool,
                 poll_ms: int = 250, clock: Callable[[], float] = time.monotonic):
        self.store = store
        self.run = run
        self.log = logger
        self.redactor = redactor
        self.guard = guard
        self.headed = headed
        self.poll_ms = poll_ms
        self.clock = clock
        self.surface: Any = None  # GuardedSurface, set by attach()
        self.capture: HumanCapture | None = None
        self.current: InterventionRequest | None = None
        self.control = ControlSession(run.run_id, [self._on_transition])
        self.store.set_control(run.run_id, self.control.state, self.control.lease.owner,
                               self.control.lease.epoch)

    # --- wiring ---------------------------------------------------------------------------------

    def attach(self, guarded_surface: Any) -> None:
        """Called once the run's GuardedSurface exists (same browser for the whole run)."""
        if self.surface is guarded_surface:
            return
        self.surface = guarded_surface
        guarded_surface.control = self.control  # lease enforced at the seam
        inner = guarded_surface.inner
        self.capture = HumanCapture(
            inner, self.redactor, self._classify, active=lambda: self.control.human_active,
            operator=lambda: self.control.lease.owner.removeprefix("human:"),
            sink=self._record_action)
        self.capture.install()
        inner.page.on("framenavigated", lambda _f: self._paint())
        self._paint()

    @property
    def inner(self) -> Any:
        return self.surface.inner if self.surface is not None else None

    def live_view(self) -> LiveView:
        inner = self.inner
        title = None
        if inner is not None and self.headed:
            try:
                title = inner.page.title()
            except Exception:
                title = None
        return LiveView(headed=self.headed, cdp_url=getattr(inner, "cdp_url", None),
                        window_title=title)

    def pump(self, ms: int | None = None) -> None:
        """Wait while letting Playwright dispatch browser events (capture, navigation)."""
        ms = self.poll_ms if ms is None else ms
        inner = self.inner
        if inner is not None:
            try:
                inner.page.wait_for_timeout(ms)
                return
            except Exception:
                pass
        time.sleep(ms / 1000)

    # --- control listeners ----------------------------------------------------------------------

    def _on_transition(self, t: Transition) -> None:
        self.log.emit("control_transition", "human" if t.actor.startswith("human:") else "system",
                      None, from_state=t.from_state, to_state=t.to_state, event=t.event,
                      by=t.actor, reason=self.redactor.redact_text(t.reason),
                      owner=t.lease.owner, epoch=t.lease.epoch)
        self.store.set_control(self.run.run_id, t.to_state, t.lease.owner, t.lease.epoch)
        self._paint()

    def _paint(self) -> None:
        """Banner in the top-most document that can render one (a frameset document cannot,
        so it goes into the first child frame: the app's banner frame)."""
        inner = self.inner
        if inner is None:
            return
        text = banner_text(self.control.state, self.run.run_id, self.control.lease.owner)
        try:
            for frame in inner.frames():
                if frame.evaluate(BANNER_JS, text):
                    break
        except Exception:
            pass  # a frame navigating away; repainted on its next framenavigated

    # --- human actions --------------------------------------------------------------------------

    def _classify(self, role: str, name: str, frame_key: str) -> str:
        """Risk only, never blocking: a human may do what policy forbids automation."""
        urls = self.inner.current_urls() if self.inner is not None else {}
        ctx = PolicyContext(mode="discovery", run_id=self.run.run_id,
                            current_url=urls.get(frame_key))
        return self.guard.check_action("click", role, name, ctx).risk

    def _record_action(self, action: HumanAction) -> None:
        data = action.model_dump(mode="json", exclude={"operator"})
        data["action"] = data.pop("type")  # `type` is the event's own field
        self.log.emit("human_action", "human", None, operator=action.operator,
                      intervention=self.current.id if self.current else None, **data)
        if self.current is not None:
            self.store.add_human_action(self.current.id, action)

    def session_check(self, stage: str) -> dict[str, str]:
        """Same-session evidence: the BrowserContext identity and a hash of the cookies."""
        inner = self.inner
        if inner is None:
            return {}
        cookies = sorted((c["name"], c["value"]) for c in inner.context.cookies())
        digest = hashlib.sha256(repr(cookies).encode()).hexdigest()[:16]
        info = {"stage": stage, "context_id": str(id(inner.context)),
                "page_id": str(id(inner.page)), "cookie_sha256_16": digest}
        self.log.emit("session_check", "system", None, **info)
        return info

    def write_live(self) -> None:
        if self.surface is not None:
            try:
                self.surface.screenshot(self.run.dir / "live.png")  # masked by GuardedSurface
            except Exception:
                pass

    # --- shared wait loop ---------------------------------------------------------------------

    def frame_paths(self) -> dict[str, str]:
        urls = self.inner.current_urls() if self.inner is not None else {}
        return {k: urlsplit(u).path for k, u in urls.items()}

    def wait_for_resolution(self, iid: str, timeout_s: float, on_claim: Callable | None = None,
                            ) -> InterventionRequest:
        """Poll the store until resolved or expired. `on_claim` runs once when claimed."""
        deadline = self.clock() + timeout_s
        claimed, last_live = False, 0.0
        while True:
            cur = self.store.get(iid)
            took = cur.status == "claimed" or (cur.status == "resolved"
                                               and cur.resolution == "resumed")
            if took and cur.operator and not claimed and on_claim is not None:
                claimed = True
                on_claim(cur)
            if cur.status in ("resolved", "expired"):
                return cur
            if self.clock() >= deadline:
                try:
                    return self.store.expire(iid)
                except Exception:
                    continue  # resolved at the last moment: re-read
            if claimed and self.clock() - last_live >= LIVE_EVERY_S:
                self.write_live()
                last_live = self.clock()
            self.pump()


class HitlEscalator:
    """Escalator: pause automation, hand the SAME live browser to an operator, wait, take it
    back. Returns resumed / handled / stop (abort or timeout)."""

    def __init__(self, session: HitlSession, timeout_s: float = 15 * 60):
        self.session = session
        self.timeout_s = timeout_s

    def attach(self, guarded_surface: Any) -> None:
        self.session.attach(guarded_surface)

    def escalate(self, request: EscalationRequest) -> EscalationDecision:
        s = self.session
        ctl = s.control
        event = "verification_failed" if ctl.state == ControlState.RESUMING else "escalate"
        ctl.transition(event, "automation", request.reason)
        kind = request.kind or ("commit_uncertain"
                                if request.category == "session_expired_after_commit"
                                else "replay_failure" if request.raised_by == "replay"
                                else "discovery_stuck")
        shot = request.evidence.get("screenshot")
        iv = InterventionRequest(
            run_id=request.run_id, kind=kind, capability=request.capability,
            goal=request.goal, step_id=request.step_id, step_intent=request.step_intent,
            reason=s.redactor.redact_text(request.reason), failure_category=request.category,
            expected=request.expected, observed=request.observed,
            screenshot=str(s.run.dir / shot) if shot else None, run_dir=str(s.run.dir),
            frame_urls=s.frame_paths(), live_view=s.live_view())
        s.store.create(s.redactor.redact(iv))
        s.current = iv
        s.log.emit("intervention", "system", request.step_id, id=iv.id, kind=kind,
                   status="open")
        s.session_check("escalated")

        def on_claim(cur: InterventionRequest) -> None:
            ctl.transition("take_control", f"human:{cur.operator}", "operator claimed",
                           owner=f"human:{cur.operator}")
            if s.capture is not None:
                s.capture.arm_current_documents()
            s.session_check("human_in_control")
            s.write_live()

        final = s.wait_for_resolution(iv.id, self.timeout_s, on_claim)
        s.current = None
        actions = final.human_actions
        base = dict(intervention_id=final.id, operator=final.operator,
                    human_action_count=len(actions),
                    human_action_summaries=[a.summary() for a in actions],
                    human_irreversible=any(a.human_irreversible for a in actions),
                    note=final.operator_note)
        s.log.emit("intervention", "human" if final.operator else "system", request.step_id,
                   id=final.id, status=final.status, resolution=final.resolution,
                   operator=final.operator, human_actions=len(actions))
        if final.status == "expired":
            ctl.transition("expired", "system", f"no operator within {self.timeout_s:.0f}s")
            return EscalationDecision(action="stop", resolution="expired", **base)
        if final.resolution == "resumed":
            ctl.transition("resume", f"human:{final.operator}", "operator resumed")
            s.session_check("resumed")
            return EscalationDecision(action="resumed", **base)
        if final.resolution == "handled":
            ctl.transition("handled", f"human:{final.operator}", final.operator_note or "")
            return EscalationDecision(action="handled", resolution=final.operator_note, **base)
        ctl.transition("aborted", f"human:{final.operator or 'operator'}", "aborted")
        return EscalationDecision(action="stop", resolution=final.resolution, **base)

    def resume_verified(self, ok: bool, rule: str | None = None) -> None:
        """Called by replay/discovery after a resume: back to AUTOMATION if the state checks out.
        (On failure the caller escalates again: RESUMING -> AWAITING_HUMAN.)"""
        if ok and self.session.control.state == ControlState.RESUMING:
            self.session.control.transition("verified", "automation", rule or "verified")


class HitlApprover:
    """Approver: an approval_required intervention; Approve -> True, Deny/timeout -> False.
    Control is not transferred: nobody touches the browser while it waits."""

    def __init__(self, session: HitlSession, timeout_s: float = 10 * 60):
        self.session = session
        self.timeout_s = timeout_s

    def attach(self, guarded_surface: Any) -> None:
        self.session.attach(guarded_surface)

    def request_approval(self, decision: PolicyDecision, action_summary: str,
                         ctx: PolicyContext) -> bool:
        s = self.session
        iv = InterventionRequest(
            run_id=s.run.run_id, kind="approval_required", step_id=ctx.step_id,
            reason=s.redactor.redact_text(f"{decision.risk} action needs approval: "
                                          f"{action_summary}"),
            approval={"decision": decision.model_dump(), "action": action_summary,
                      "mode": ctx.mode},
            run_dir=str(s.run.dir), frame_urls=s.frame_paths(), live_view=s.live_view())
        s.store.create(s.redactor.redact(iv))
        s.log.emit("intervention", "system", ctx.step_id, id=iv.id, kind=iv.kind,
                   status="open")
        final = s.wait_for_resolution(iv.id, self.timeout_s)
        approved = final.status == "resolved" and final.resolution == "approved"
        s.log.emit("intervention", "human" if final.operator else "system", ctx.step_id,
                   id=iv.id, status=final.status, resolution=final.resolution,
                   operator=final.operator, approved=approved)
        return approved


def screenshot_path(iv: InterventionRequest) -> Path | None:
    return Path(iv.screenshot) if iv.screenshot else None
