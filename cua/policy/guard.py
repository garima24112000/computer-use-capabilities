"""PolicyGuard: pure allowlist / risk / verdict evaluation, plus the Approver seam."""

import re
from fnmatch import fnmatch
from typing import Protocol
from urllib.parse import urlsplit

from cua.policy.models import (
    ConditionalVerdict,
    PolicyConfig,
    PolicyContext,
    PolicyDecision,
    Risk,
    RiskMatch,
)


def _as_list(v: str | list[str] | None) -> list[str] | None:
    return [v] if isinstance(v, str) else v


def url_path(url: str) -> str:
    return urlsplit(url).path or "/"


class PolicyGuard:
    """Stateless policy evaluation. No browser access, so everything is unit-testable."""

    def __init__(self, config: PolicyConfig):
        self.config = config
        self._name_res = {r.id: re.compile(r.match.name_regex) for r in config.risk_rules
                          if r.match.name_regex}

    # --- URLs -----------------------------------------------------------------------------------

    def url_decision(self, url: str) -> PolicyDecision | None:
        """A block decision if `url` is outside the allowlist, else None."""
        parts = urlsplit(url)
        if parts.scheme in ("about", "data") or url == "about:blank":
            return None
        if parts.scheme not in ("http", "https"):
            return _block("scheme_not_allowed", f"scheme {parts.scheme!r} is not allowed")
        origin = f"{parts.scheme}://{parts.netloc}"
        if not any(fnmatch(origin, pat) for pat in self.config.allowed_origins):
            return _block("origin_not_allowlisted", f"origin {origin} is not allowlisted")
        path = parts.path or "/"
        for pat in self.config.blocked_paths:
            if fnmatch(path, pat):
                return _block("blocked_path", f"path {path} matches blocked pattern {pat}")
        if not any(fnmatch(path, pat) for pat in self.config.allowed_paths):
            return _block("path_not_allowlisted", f"path {path} is not allowlisted")
        return None

    def check_navigation(self, url: str, ctx: PolicyContext) -> PolicyDecision:
        blocked = self.url_decision(url)
        if blocked is not None:
            return blocked
        return self._decide("navigate", None, None, url_path(url), ctx)

    # --- actions --------------------------------------------------------------------------------

    def check_action(self, action: str, element_role: str | None, element_name: str | None,
                     ctx: PolicyContext) -> PolicyDecision:
        if action not in self.config.allowed_actions:
            return _block("action_not_allowed", f"action {action!r} is not in allowed_actions")
        path = None
        if ctx.current_url:
            blocked = self.url_decision(ctx.current_url)
            if blocked is not None:
                return blocked.model_copy(update={
                    "reason": f"current page not allowed: {blocked.reason}"})
            path = url_path(ctx.current_url)
        return self._decide(action, element_role, element_name, path, ctx)

    def _decide(self, action: str, role: str | None, name: str | None, path: str | None,
                ctx: PolicyContext) -> PolicyDecision:
        risk: Risk = self.config.default_risk.get(action, "risky")  # type: ignore[arg-type]
        rule_id, reason = f"default:{action}", f"default risk for {action}"
        for rule in self.config.risk_rules:
            if self._matches(rule.id, rule.match, action, role, name, path):
                risk, rule_id, reason = rule.risk, rule.id, rule.reason or rule.id
                break
        return PolicyDecision(verdict=self._verdict(risk, ctx), risk=risk, rule_id=rule_id,
                              reason=reason)

    def _matches(self, rule_id: str, m: RiskMatch, action: str, role: str | None,
                 name: str | None, path: str | None) -> bool:
        actions, roles = _as_list(m.action), _as_list(m.role)
        if actions is not None and action not in actions:
            return False
        if roles is not None and role not in roles:
            return False
        if m.name_regex is not None and not self._name_res[rule_id].search(name or ""):
            return False
        if m.url_glob is not None and (path is None or not fnmatch(path, m.url_glob)):
            return False
        return True

    def _verdict(self, risk: Risk, ctx: PolicyContext):
        rule = self.config.mode_rules[ctx.mode][risk]
        if isinstance(rule, ConditionalVerdict):
            ok = all(getattr(ctx, flag) for flag in rule.requires)
            return rule.verdict if ok else rule.otherwise
        return rule


def _block(rule_id: str, reason: str) -> PolicyDecision:
    return PolicyDecision(verdict="block", risk="irreversible", rule_id=rule_id, reason=reason)


# --- approvals -----------------------------------------------------------------------------------


class Approver(Protocol):
    def request_approval(self, decision: PolicyDecision, action_summary: str,
                         ctx: PolicyContext) -> bool: ...


class DenyAllApprover:
    """Default: nothing that needs approval is approved."""

    def request_approval(self, decision: PolicyDecision, action_summary: str,
                         ctx: PolicyContext) -> bool:
        return False


class AutoApproveForTests:
    """Approves everything and records what it was asked. Never use outside tests/demos."""

    def __init__(self) -> None:
        self.requests: list[tuple[PolicyDecision, str]] = []

    def request_approval(self, decision: PolicyDecision, action_summary: str,
                         ctx: PolicyContext) -> bool:
        self.requests.append((decision, action_summary))
        return True
