"""Artifact lint: every rule runs and every problem is reported, not just the first.

Errors make the model invalid: Capability and AppProfile run lint in their model validators.
Warnings are reported by `store.load()` and `cua artifact validate`, but they do not block.

The sensitive-data rules use the same Redactor as the runtime. A string that the redactor would
change, such as an SSN shape, a card number, an email or a phone number, must not appear in an
artifact. The structural rule (typing a literal into a field labelled PASSWORD or SSN) uses the
profile's sensitive labels, passed in through the validation context.
"""

import re
from collections import Counter
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from cua.artifact.conditions import Checkpoint, TargetAbsent, TargetPresent, UrlMatches
from cua.artifact.steps import Extract, Fill, Navigate, Press, Select, Step, action_targets
from cua.policy.redaction import Redactor, SensitiveLabels
from cua.surface.locators import CssPath, LabelAnchor, RoleName, TableCell, TargetSpec, TextMatch
from cua.templating import placeholders

if TYPE_CHECKING:
    from cua.artifact.contract import InputParam
    from cua.artifact.profile import AppProfile
    from cua.artifact.schema import Capability

# Always treated as sensitive, even when no profile labels are supplied.
DEFAULT_SENSITIVE_LABELS = ["PASSWORD", "SSN", "SOCIAL SECURITY", "DOB", "DATE OF BIRTH"]
_PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}")


class LintIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


class LintReport(BaseModel):
    errors: list[LintIssue] = Field(default_factory=list)
    warnings: list[LintIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> set[str]:
        return {i.code for i in self.errors + self.warnings}

    def format(self) -> str:
        lines = [f"ERROR {i}" for i in self.errors] + [f"WARN  {i}" for i in self.warnings]
        return "\n".join(lines) or "OK"


def labels_from_context(context: Any) -> list[str]:
    labels = (context or {}).get("sensitive_labels") if isinstance(context, dict) else None
    return list(labels or [])


class _Linter:
    def __init__(self, labels: list[str], inputs: "dict[str, InputParam] | None",
                 secrets: set[str]):
        self.redactor = Redactor()  # pattern layer: SSN/email/card/phone/long digit runs
        self.labels = SensitiveLabels(DEFAULT_SENSITIVE_LABELS + labels)
        self.inputs = inputs  # None = no inputs allowed (app profiles)
        self.secrets = secrets
        self.report = LintReport()
        self.used_inputs: set[str] = set()

    def err(self, code: str, path: str, message: str) -> None:
        self.report.errors.append(LintIssue(severity="error", code=code, path=path,
                                            message=message))

    def warn(self, code: str, path: str, message: str) -> None:
        self.report.warnings.append(LintIssue(severity="warning", code=code, path=path,
                                              message=message))

    # --- building blocks ------------------------------------------------------------------------

    def duplicates(self, code: str, path: str, names: Iterable[str]) -> None:
        for name, n in Counter(names).items():
            if n > 1:
                self.err(code, path, f"{name!r} is declared {n} times")

    def sensitive_text(self, code: str, path: str, text: str) -> None:
        literal = _PLACEHOLDER_RE.sub("", text)
        if self.redactor.redact_text(literal) != literal:
            self.err(code, path, "contains a value that looks sensitive (the redactor would "
                                 f"change it): {self.redactor.redact_text(literal)!r}")

    def template(self, path: str, text: str, allow_secret: bool) -> None:
        for p in placeholders(text):  # grammar already validated by the field type
            if p.kind == "input":
                self.used_inputs.add(p.name)
                if self.inputs is None:
                    self.err("input_not_allowed", path,
                             f"{p.raw}: app profiles have no inputs")
                elif p.name not in self.inputs:
                    self.err("undeclared_input", path, f"{p.raw} is not a declared input")
            elif not allow_secret:
                self.err("secret_not_allowed", path, f"{p.raw}: secrets are only allowed as "
                                                     "a whole Fill value")
            elif p.name not in self.secrets:
                self.err("undeclared_secret", path, f"{p.raw} is not declared in `secrets`")

    def target(self, path: str, t: TargetSpec) -> None:
        dumped = t.model_dump(mode="json", exclude_none=True)
        for i, s in enumerate(t.strategies):
            spath = f"{path}.strategies[{i}]"
            for key, value in s.model_dump(mode="json").items():
                if not isinstance(value, str) or "{{" not in value:
                    continue
                if isinstance(s, TableCell) and key == "row_key" and s.row_key_input:
                    continue
                self.err("template_in_target", f"{spath}.{key}",
                         "templates are only allowed as a whole TableCell.row_key "
                         "({{inputs.<name>}})")
        for name in t.input_names:
            self.used_inputs.add(name)
            if self.inputs is None:
                self.err("input_not_allowed", path, f"row_key uses {{{{inputs.{name}}}}}: app "
                                                    "profiles have no inputs")
            elif name not in self.inputs:
                self.err("undeclared_input", path, f"row_key uses undeclared input {name!r}")
            elif self.inputs[name].sensitive or not self.inputs[name].required:
                self.err("parameterized_target_input", path,
                         f"row_key input {name!r} must be required and non-sensitive")
        if self.redactor.redact(dumped) != dumped:
            self.err("sensitive_in_target", path,
                     "target contains a value that looks sensitive (redactor would change it)")
        if t.strategies and all(isinstance(s, CssPath) for s in t.strategies):
            self.warn("brittle_target", path, "only css_path strategies: breaks on any layout "
                                              "change")
        if not t.strategies:
            self.err("empty_target", path, "target has no strategies")

    def target_is_sensitive_field(self, t: TargetSpec) -> str | None:
        for s in t.strategies:
            text = s.label if isinstance(s, LabelAnchor) else (
                s.name if isinstance(s, RoleName) else s.text if isinstance(s, TextMatch) else None)
            if text and (kind := self.labels.kind_for(text)):
                return kind
        return None

    def checkpoint(self, path: str, cp: Checkpoint) -> None:
        for rel, c in cp.conditions():
            cpath = f"{path}.{rel}"
            if isinstance(c, TargetPresent | TargetAbsent):
                self.target(f"{cpath}.target", c.target)
            elif not isinstance(c, UrlMatches):
                self.template(f"{cpath}.text", c.text, allow_secret=False)
                self.sensitive_text("sensitive_in_checkpoint", f"{cpath}.text", c.text)

    def steps(self, path: str, steps: list[Step], outputs: set[str] | None) -> Counter:
        """Lint a step list; returns how often each output is extracted."""
        self.duplicates("duplicate_step_id", path, (s.id for s in steps))
        extracted: Counter = Counter()
        for i, step in enumerate(steps):
            spath = f"{path}[{i}:{step.id}]"
            a = step.action
            for rel, t in action_targets(a):
                self.target(f"{spath}.action.{rel}", t)
            if isinstance(a, Navigate):
                self.template(f"{spath}.action.url_path", a.url_path, allow_secret=False)
            elif isinstance(a, Fill):
                self.fill(spath, a)
            elif isinstance(a, Select):
                self.template(f"{spath}.action.option", a.option, allow_secret=False)
                self.sensitive_text("sensitive_literal", f"{spath}.action.option", a.option)
            elif isinstance(a, Extract):
                if outputs is None:
                    self.err("extract_not_allowed", spath, "app profile steps cannot extract")
                elif a.output not in outputs:
                    self.err("extract_undeclared_output", spath,
                             f"extracts into undeclared output {a.output!r}")
                extracted[a.output] += 1
            elif isinstance(a, Press) and a.key in ("Enter", "Return", "NumpadEnter") \
                    and step.risk == "safe":
                self.warn("press_enter_marked_safe", spath,
                          "Enter can submit a form; it is classified live at replay")
            if step.risk == "irreversible" and not step.requires_approval:
                self.err("irreversible_without_approval", spath,
                         "irreversible steps must have requires_approval=true")
            if step.approved and not step.requires_approval:
                self.warn("approved_without_gate", spath,
                          "approved=true on a step that does not require approval")
            if step.expect is not None:
                self.checkpoint(f"{spath}.expect", step.expect)
            if step.risk != "safe" and step.expect is None:
                self.warn("unverified_step", spath, f"{step.risk} step has no `expect` "
                                                    "postcondition")
        return extracted

    def fill(self, spath: str, a: Fill) -> None:
        vpath = f"{spath}.action.value"
        self.template(vpath, a.value, allow_secret=True)
        self.sensitive_text("sensitive_literal", vpath, a.value)
        kind = self.target_is_sensitive_field(a.target)
        if kind is None:
            return
        ps = placeholders(a.value)
        literal = _PLACEHOLDER_RE.sub("", a.value).strip()
        if literal:
            self.err("sensitive_literal", vpath, f"literal text typed into a {kind} field; use "
                                                 "a sensitive input or a {{secret:NAME}}")
        for p in ps:
            if p.kind == "input" and self.inputs and p.name in self.inputs \
                    and not self.inputs[p.name].sensitive:
                self.err("sensitive_field_input_not_sensitive", vpath,
                         f"input {p.name!r} fills a {kind} field and must be sensitive=true")


def lint_capability(cap: "Capability", labels: list[str] | None = None) -> LintReport:
    inputs = {i.name: i for i in cap.inputs}
    lin = _Linter(labels or [], inputs, {s.name for s in cap.secrets})
    lin.duplicates("duplicate_input", "inputs", (i.name for i in cap.inputs))
    lin.duplicates("duplicate_output", "outputs", (o.name for o in cap.outputs))
    lin.duplicates("duplicate_secret", "secrets", (s.name for s in cap.secrets))
    lin.duplicates("duplicate_outcome", "outcomes",
                   [o.id for o in cap.outcomes] + list(cap.use_profile_outcomes))
    lin.duplicates("duplicate_outcome_code", "outcomes", (o.code for o in cap.outcomes))

    outputs = {o.name for o in cap.outputs}
    extracted = lin.steps("steps", cap.steps, outputs)
    for name in sorted(outputs):
        if extracted[name] == 0:
            lin.err("output_not_extracted", f"outputs.{name}", "no Extract step produces it")
        elif extracted[name] > 1:
            lin.err("output_extracted_multiple", f"outputs.{name}",
                    f"extracted by {extracted[name]} steps (must be exactly one)")

    step_ids = {s.id for s in cap.steps}
    for i, o in enumerate(cap.outcomes):
        opath = f"outcomes[{i}:{o.id}]"
        if o.check_after_steps != "*":
            for sid in o.check_after_steps:
                if sid not in step_ids:
                    lin.err("unknown_step_reference", f"{opath}.check_after_steps",
                            f"step {sid!r} does not exist")
        lin.template(f"{opath}.message_template", o.message_template, allow_secret=False)
        lin.sensitive_text("sensitive_in_checkpoint", f"{opath}.message_template",
                           o.message_template)
        lin.checkpoint(f"{opath}.detect", o.detect)
    lin.checkpoint("success", cap.success)

    for name, param in inputs.items():
        if param.required and name not in lin.used_inputs:
            lin.err("unused_required_input", f"inputs.{name}",
                    "required input is never used by any step, target or checkpoint")
        if param.example is not None:
            lin.sensitive_text("sensitive_example", f"inputs.{name}.example", param.example)
    lin.sensitive_text("sensitive_in_provenance", "provenance.goal_redacted",
                       cap.provenance.goal_redacted)
    _lint_extract_stability(lin, cap)
    _lint_input_literals(lin, cap)

    if cap.status == "approved":
        for i, s in enumerate(cap.steps):
            if s.requires_approval and not s.approved:
                lin.err("approval_missing", f"steps[{i}:{s.id}]",
                        "status=approved but this gated step is not approved "
                        "(run `cua artifact approve`)")
        if cap.review.approved_at is None or not cap.review.reviewed_by:
            lin.err("review_missing", "review", "status=approved requires reviewed_by and "
                                                "approved_at")
        if cap.review.approved_content_hash is None:
            lin.err("review_missing", "review.approved_content_hash",
                    "status=approved requires the approved content hash")
        elif cap.review.approved_content_hash != cap.content_hash():
            lin.warn("approval_stale", "review.approved_content_hash",
                     "content changed since approval; replay will refuse irreversible steps "
                     "until re-approved")
    return lin.report


def _lint_extract_stability(lin: _Linter, cap: "Capability") -> None:
    """Every Extract needs a non-structural strategy: a css_path alone breaks on any layout
    change. Discovered artifacts are rejected for it; hand-written ones get a warning."""
    for i, step in enumerate(cap.steps):
        if isinstance(step.action, Extract) and all(
                isinstance(s, CssPath) for s in step.action.target.strategies):
            path = f"steps[{i}:{step.id}].action.target"
            msg = ("extract target has only css_path strategies; it needs a TableCell, "
                   "LabelAnchor or stable TextMatch (with the output's extract_pattern)")
            if cap.provenance.source == "discovery":
                lin.err("extract_structural_only", path, msg)
            else:
                lin.warn("extract_structural_only", path, msg)


def _lint_input_literals(lin: _Linter, cap: "Capability") -> None:
    """Free text should say {{inputs.x}}, not the value used during discovery."""
    values = {p.name: p.example for p in cap.inputs
              if p.example and not p.sensitive and len(p.example) >= 3}
    if not values:
        return
    texts: list[tuple[str, str]] = [("title", cap.title), ("description", cap.description)]
    for i, s in enumerate(cap.steps):
        sp = f"steps[{i}:{s.id}]"
        texts += [(f"{sp}.intent", s.intent), (f"{sp}.notes", s.notes or "")]
        if (t := getattr(s.action, "target", None)) is not None:
            texts.append((f"{sp}.action.target.description", t.description))
        if s.expect is not None:
            texts.append((f"{sp}.expect.description", s.expect.description))
    texts.append(("success.description", cap.success.description))
    for path, text in texts:
        for name, value in values.items():
            if re.search(rf"(?<![\w.]){re.escape(value)}(?![\w])", text):
                lin.warn("input_literal_in_text", path,
                         f"contains the discovery value of input {name!r}; "
                         f"write {{{{inputs.{name}}}}} instead")


def lint_profile(profile: "AppProfile") -> LintReport:
    lin = _Linter(profile.sensitive_labels, None, {s.name for s in profile.secrets})
    lin.duplicates("duplicate_secret", "secrets", (s.name for s in profile.secrets))
    lin.steps("login_subflow", profile.login_subflow, None)
    lin.duplicates("duplicate_interstitial", "interstitials", (i.id for i in profile.interstitials))
    for i, inter in enumerate(profile.interstitials):
        ipath = f"interstitials[{i}:{inter.id}]"
        lin.checkpoint(f"{ipath}.detect", inter.detect)
        for j, action in enumerate(inter.recover):
            for rel, t in action_targets(action):
                lin.target(f"{ipath}.recover[{j}].{rel}", t)
            if isinstance(action, Extract):
                lin.err("extract_not_allowed", f"{ipath}.recover[{j}]", "recovery cannot extract")
    lin.duplicates("duplicate_dialog", "known_dialogs", (d.id for d in profile.known_dialogs))
    lin.checkpoint("session_expired.detect", profile.session_expired.detect)
    lin.duplicates("duplicate_error_signature", "error_signatures",
                   (e.id for e in profile.error_signatures))
    for i, sig in enumerate(profile.error_signatures):
        lin.checkpoint(f"error_signatures[{i}:{sig.id}].detect", sig.detect)
    lin.duplicates("duplicate_outcome", "outcomes", (o.id for o in profile.outcomes))
    lin.duplicates("duplicate_outcome_code", "outcomes", (o.code for o in profile.outcomes))
    for i, o in enumerate(profile.outcomes):
        opath = f"outcomes[{i}:{o.id}]"
        if o.check_after_steps != "*":
            lin.err("unknown_step_reference", f"{opath}.check_after_steps",
                    "profile outcomes apply to any capability: use '*'")
        lin.template(f"{opath}.message_template", o.message_template, allow_secret=False)
        lin.checkpoint(f"{opath}.detect", o.detect)
    if not profile.sensitive_labels:
        lin.err("no_sensitive_labels", "sensitive_labels", "a profile must declare its PII "
                                                           "labels")
    return lin.report
