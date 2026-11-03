"""GuardedSurface: a Surface decorator enforcing policy, redaction and event logging.

Discovery and replay are handed a GuardedSurface, never the raw surface:
- every action is checked by PolicyGuard (block / require_approval / allow) and logged;
- every observation, read and description leaving it is redacted;
- `{{secret:NAME}}` fills are resolved here and only the template is ever logged;
- every network request is filtered against the allowlist (where the inner surface supports it);
- screenshots always mask sensitive cells, failing closed to whole-frame masks.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from cua.obs.events import Actor, EventLogger
from cua.policy.errors import ApprovalDenied, PolicyBlocked
from cua.policy.guard import Approver, DenyAllApprover, PolicyGuard
from cua.policy.models import PolicyContext, PolicyDecision
from cua.policy.redaction import (
    Redactor,
    SecretResolver,
    element_sensitivity,
    sensitive_mask_targets,
    token,
)
from cua.surface.base import Surface, Target
from cua.surface.errors import ActionFailed, SurfaceError, TargetNotFound
from cua.surface.locators import CssPath, TargetSpec
from cua.surface.models import (
    TOP_FRAME_KEY,
    ActionResult,
    ElementHandleRef,
    ElementInfo,
    FrameRef,
    KeyContext,
    Observation,
    ResolveResult,
)

_VERDICT_RANK = {"allow": 0, "require_approval": 1, "block": 2}
_RISK_RANK = {"safe": 0, "risky": 1, "irreversible": 2}
SUBMIT_KEYS = {"Enter", "NumpadEnter", "Return"}  # can submit the focused element's form
ACTIVATE_KEYS = {" ", "Space"}  # clicks a focused button/link

Decide = Callable[[str, "_Info", PolicyContext], PolicyDecision]


def _strictest(decisions: list[PolicyDecision]) -> PolicyDecision:
    return max(decisions, key=lambda d: (_VERDICT_RANK[d.verdict], _RISK_RANK[d.risk]))


class GuardedActionResult(ActionResult):
    policy: PolicyDecision


def fail_closed_masks(surface: Surface, targets: list[TargetSpec]) -> list[TargetSpec]:
    """Keep resolvable mask targets; replace unresolvable ones with a whole-frame mask."""
    out = []
    for t in targets:
        try:
            surface.resolve(t)
            out.append(t)
        except TargetNotFound:
            out.append(TargetSpec(frame=t.frame, strategies=[CssPath(selector="html")],
                                  description=f"whole-frame mask (fail closed) [{t.frame.key}]"))
    return out


class _Info:
    """What the guard knows about an action's target."""

    def __init__(self, el: ElementInfo | None, frame_key: str, fingerprint=None):
        self.el = el
        self.frame_key = frame_key
        self.fingerprint = fingerprint

    @property
    def role(self) -> str | None:
        return self.el.role if self.el else (self.fingerprint.role if self.fingerprint else None)

    @property
    def name(self) -> str | None:
        return self.el.name if self.el else (self.fingerprint.name if self.fingerprint else None)


class GuardedSurface(Surface):
    masks_sensitive = True  # EvidenceCapture relies on screenshot() masking by itself

    def __init__(
        self,
        inner: Surface,
        guard: PolicyGuard,
        ctx_provider: Callable[[], PolicyContext],
        event_logger: EventLogger,
        approver: Approver | None = None,
        redactor: Redactor | None = None,
        secrets: SecretResolver | None = None,
    ):
        self.inner = inner
        self.guard = guard
        self.ctx_provider = ctx_provider
        self.log = event_logger
        self.approver = approver or DenyAllApprover()
        self.redactor = redactor or event_logger.redactor
        self.secrets = secrets or SecretResolver(self.redactor)
        self.labels = guard.config.sensitive_labels
        self._elements: dict[str, ElementInfo] = {}
        if hasattr(inner, "set_request_filter"):
            inner.set_request_filter(lambda url: guard.url_decision(url) is None,
                                     self._on_network_blocked)

    # --- helpers --------------------------------------------------------------------------------

    def _actor(self, ctx: PolicyContext) -> Actor:
        return "agent" if ctx.mode == "discovery" else "replay"

    def _on_network_blocked(self, url: str, resource_type: str) -> None:
        ctx = self.ctx_provider()
        decision = self.guard.url_decision(url)
        self.log.emit("network_blocked", self._actor(ctx), ctx.step_id, url=url,
                      resource_type=resource_type, decision=decision)

    def _learn(self, el: ElementInfo | None) -> None:
        """Register an element's name as sensitive if its table position says it is PII."""
        if el is not None and (kind := element_sensitivity(el, self.redactor.labels)):
            self.redactor.register_sensitive(el.name, kind)

    def _info(self, action: str, target: Target | None) -> _Info:
        if target is None:
            return _Info(None, TOP_FRAME_KEY)
        if isinstance(target, str):
            el = self._elements.get(target)
            if el is None:
                raise ActionFailed(action, target, "unknown ref; call observe() first")
            self._learn(el)
            return _Info(el, el.frame.key)
        handle, _ = self.inner.resolve(target)  # raises TargetNotFound (logged by caller)
        self._learn(handle.element)
        return _Info(handle.element, target.frame.key, target.fingerprint)

    def _summary(self, action: str, info: _Info, target: Target | None) -> str:
        desc = target.description if isinstance(target, TargetSpec) else (target or "page")
        return self.redactor.redact_text(
            f'{action} {info.role or "?"} "{info.name or ""}" [{info.frame_key}] ({desc})')

    def _decide(self, action: str, info: _Info, ctx: PolicyContext) -> PolicyDecision:
        decisions = [self.guard.check_action(action, info.role, info.name, ctx)]
        fp = info.fingerprint
        if fp is not None and info.el is not None and (fp.role, fp.name) != (info.role, info.name):
            # Live element differs from the recorded one (drift): the stricter decision wins.
            decisions.append(self.guard.check_action(action, fp.role, fp.name, ctx))
        return _strictest(decisions)

    def _press_decider(self, key: str, target: Target | None) -> Decide:
        """Enter is classified like a click on the form submit control it would trigger.

        Unknown submit control -> risky. A control whose name matches an irreversible rule (e.g.
        the commit-verb rule or SUBMIT on the review page) makes the press irreversible.
        """
        base_key = key.split("+")[-1]

        def decide(action: str, info: _Info, ctx: PolicyContext) -> PolicyDecision:
            base = self._decide(action, info, ctx)
            if base.verdict == "block" or base_key not in SUBMIT_KEYS | ACTIVATE_KEYS:
                return base
            kc: KeyContext = self.inner.key_context(target)
            self._learn(kc.focused)
            for c in kc.submit_controls:
                self._learn(c)
            if base_key in ACTIVATE_KEYS and kc.known and (
                    kc.focused is None or kc.focused.role not in ("button", "link")):
                return base  # typing a space into a field
            if not kc.known or not kc.submit_controls:
                why = kc.note or "focused element has no known form submit control"
                return _strictest([base, self.guard.decision_for(
                    "risky", "press-submit:unknown", f"{key} may submit a form ({why})", ctx)])
            decisions = [base]
            for c in kc.submit_controls:
                cctx = ctx.model_copy(update={"current_url":
                                              self.inner.current_urls().get(c.frame.key)})
                d = self.guard.check_action("click", c.role, c.name, cctx)
                if d.verdict == "block":
                    decisions.append(d)
                    continue
                risk = d.risk if _RISK_RANK[d.risk] >= _RISK_RANK["risky"] else "risky"
                decisions.append(self.guard.decision_for(
                    risk, f"press-submit:{d.rule_id}",
                    self.redactor.redact_text(
                        f'{key} triggers {c.role} "{c.name}" [{c.frame.key}]: {d.reason}'),
                    cctx))
            return _strictest(decisions)

        return decide

    def _authorize(self, decision: PolicyDecision, summary: str, ctx: PolicyContext) -> None:
        actor = self._actor(ctx)
        self.log.emit("policy_decision", actor, ctx.step_id, action=summary, decision=decision)
        if decision.verdict == "block":
            raise PolicyBlocked(decision, summary)
        if decision.verdict == "require_approval":
            self.log.emit("approval_requested", actor, ctx.step_id, action=summary,
                          decision=decision)
            approved = bool(self.approver.request_approval(decision, summary, ctx))
            self.log.emit("approval_result", actor, ctx.step_id, action=summary,
                          approved=approved, approver=type(self.approver).__name__)
            if not approved:
                raise ApprovalDenied(decision, summary)

    def _guarded(self, action: str, target: Target | None, run: Callable[[_Info], ActionResult],
                 log_extra: Callable[[], dict[str, Any]] | None = None,
                 decide: Decide | None = None, **log_data: Any) -> GuardedActionResult:
        base = self.ctx_provider()
        actor = self._actor(base)
        try:
            info = self._info(action, target)
        except SurfaceError as exc:
            self.log.emit("error", actor, base.step_id, action=action, error=str(exc))
            raise
        url = self.inner.current_urls().get(info.frame_key)
        ctx = base.model_copy(update={"current_url": url})
        summary = self._summary(action, info, target)
        decision = (decide or self._decide)(action, info, ctx)
        self._authorize(decision, summary, ctx)
        try:
            result = run(info)
        except SurfaceError as exc:
            self.log.emit("error", actor, ctx.step_id, action=summary, error=str(exc))
            raise
        self.log.emit("action", actor, ctx.step_id, action=summary, ok=result.ok,
                      navigated=result.navigated, duration_ms=round(result.duration_ms, 1),
                      resolve=result.resolve_result, **log_data,
                      **(log_extra() if log_extra else {}))
        for d in result.dialogs:
            self.log.emit("dialog", actor, ctx.step_id, dialog=d)
        return GuardedActionResult(**result.model_dump(), policy=decision)

    def _sensitive_kind(self, info: _Info) -> str | None:
        el = info.el
        if el is None:
            return None
        if el.input_type == "password":
            return "SECRET"
        return self.redactor.labels.kind_for(el.label or el.name)

    # --- lifecycle / navigation -----------------------------------------------------------------

    def open(self, url: str) -> None:
        ctx = self.ctx_provider()
        actor = self._actor(ctx)
        decision = self.guard.check_navigation(url, ctx)
        self._authorize(decision, f"navigate {url}", ctx)
        try:
            self.inner.open(url)
        except SurfaceError as exc:
            self.log.emit("error", actor, ctx.step_id, action=f"navigate {url}", error=str(exc))
            raise
        self.log.emit("action", actor, ctx.step_id, action=f"navigate {url}", ok=True)

    def close(self) -> None:
        self.inner.close()

    def start_trace(self, path: str | Path) -> None:
        self.inner.start_trace(path)  # type: ignore[attr-defined]

    def stop_trace(self) -> Path | None:
        return self.inner.stop_trace()  # type: ignore[attr-defined]

    # --- perceive -------------------------------------------------------------------------------

    def _observe_raw(self) -> Observation:
        raw = self.inner.observe()
        self._elements = {e.ref: e for e in raw.elements if e.ref}
        return raw

    def observe(self) -> Observation:
        ctx = self.ctx_provider()
        redacted = self.redactor.redact_observation(self._observe_raw(), self.labels)
        self.log.emit("observation", self._actor(ctx), ctx.step_id,
                      frames=[{"frame": f.frame.key, "url": f.url, "title": f.title}
                              for f in redacted.frames],
                      element_count=len(redacted.elements), dialogs=redacted.dialogs)
        return redacted

    def describe(self, ref: str, description: str | None = None) -> TargetSpec:
        """Like the inner describe(), minus any strategy that would embed a sensitive value."""
        spec = self.inner.describe(ref, description)
        safe = [s for s in spec.strategies if self.redactor.redact(s) == s]
        return TargetSpec(
            frame=spec.frame, strategies=safe,
            description=self.redactor.redact_text(spec.description),
            fingerprint=self.redactor.redact(spec.fingerprint) if spec.fingerprint else None,
        )

    def resolve(self, target: TargetSpec) -> tuple[ElementHandleRef, ResolveResult]:
        handle, result = self.inner.resolve(target)
        self._learn(handle.element)
        return self.redactor.redact(handle), result

    # --- act ------------------------------------------------------------------------------------

    def click(self, target: Target, *, settle: bool = True) -> GuardedActionResult:
        return self._guarded("click", target, lambda _: self.inner.click(target, settle=settle))

    def fill(self, target: Target, value: str, *, settle: bool = True) -> GuardedActionResult:
        """Fill a field. `{{secret:NAME}}` is resolved here; only the template is logged.

        Values typed into sensitive fields (password inputs, sensitive labels) are registered with
        the redactor and logged as a redaction token.
        """
        logged = {"value": value}

        def run(info: _Info) -> ActionResult:
            if self.secrets.is_template(value):
                real = self.secrets.resolve(value)  # registered with the redactor first
            else:
                real = value
                kind = self._sensitive_kind(info)
                if kind:
                    self.redactor.register_sensitive(value, kind)
                    logged["value"] = token(kind)
            return self.inner.fill(target, real, settle=settle)

        return self._guarded("fill", target, run, log_extra=lambda: logged)

    def select(self, target: Target, option_label: str, *, settle: bool = True
               ) -> GuardedActionResult:
        return self._guarded("select", target,
                             lambda _: self.inner.select(target, option_label, settle=settle),
                             option=option_label)

    def press(self, key: str, target: Target | None = None, *, settle: bool = True
              ) -> GuardedActionResult:
        """Enter/Space are classified by what they would trigger (see _press_decider)."""
        return self._guarded("press", target,
                             lambda _: self.inner.press(key, target, settle=settle),
                             decide=self._press_decider(key, target), key=key)

    def key_context(self, target: Target | None = None) -> KeyContext:
        kc = self.inner.key_context(target)
        self._learn(kc.focused)
        return self.redactor.redact(kc)

    # --- read -----------------------------------------------------------------------------------

    def read_text(self, target: Target) -> str:
        holder: dict[str, str] = {}

        def run(info: _Info) -> ActionResult:
            text = self.inner.read_text(target)
            kind = element_sensitivity(info.el, self.redactor.labels) if info.el else None
            if kind:
                self.redactor.register_sensitive(text, kind)
                text = token(kind)
            holder["text"] = self.redactor.redact_text(text)
            return ActionResult(ok=True, duration_ms=0.0)

        self._guarded("read", target, run)
        return holder["text"]

    def frame_text(self, frame: FrameRef | None = None) -> str:
        return self.redactor.redact_text(self.inner.frame_text(frame))

    def page_contains(self, text: str, frame: FrameRef | None = None) -> bool:
        return self.inner.page_contains(text, frame)

    def screenshot(self, path: str | Path, mask_targets: list[TargetSpec] | None = None) -> Path:
        """Always masks sensitive cells (from a fresh observation), plus any extra targets.

        Note: this re-observes, so refs from an earlier observe() may be renumbered.
        """
        raw = self._observe_raw()
        self.redactor.redact_observation(raw, self.labels)  # registers newly seen values
        masks = sensitive_mask_targets(raw, self.labels) + list(mask_targets or [])
        return self.inner.screenshot(path, fail_closed_masks(self.inner, masks))

    def current_urls(self) -> dict[str, str]:
        return self.redactor.redact(self.inner.current_urls())

    def wait_for_settle(self, timeout_ms: float | None = None) -> None:
        self.inner.wait_for_settle(timeout_ms)
