"""Recorder: turns the successful actions of a discovery run into a draft Capability.

Everything it stores comes from PII-safe sources: GuardedSurface.describe() (sensitive
strategies already dropped), redacted observations and the agent's templated values. Before
saving, the whole artifact is checked against the run's Redactor. Any value the run learned was
sensitive (e.g. a member name) rejects the artifact, even if the pattern-based lint would not
notice.
"""

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ValidationError

from cua import __version__
from cua.artifact import store
from cua.artifact.conditions import Checkpoint, Condition, TargetPresent, TextPresent, UrlMatches
from cua.artifact.contract import InputParam, OutputField
from cua.artifact.profile import AppProfile
from cua.artifact.schema import Capability, Provenance
from cua.artifact.steps import Click, Extract, Fill, Press, Select, Step
from cua.discovery.models import DiscoveryGoal
from cua.discovery.prompts import PROMPT_VERSION
from cua.discovery.tools import ValidatedCall
from cua.execute import check_condition
from cua.obs.events import EventLogger
from cua.obs.run import RunContext
from cua.policy.models import PolicyDecision
from cua.policy.redaction import Redactor
from cua.surface.base import Surface
from cua.surface.errors import SurfaceError, TargetNotFound
from cua.surface.locators import CssPath, LabelAnchor, RoleName, TableCell, TargetSpec, TextMatch
from cua.surface.models import ElementInfo, Observation
from cua.templating import placeholders

RECORDER_VERSION = f"recorder-v1+{PROMPT_VERSION}"
_ID_LIKE = re.compile(r"^(\d+|[0-9a-fA-F]{8,}|[0-9a-fA-F-]{32,})$")
_UNSTABLE = re.compile(r"\d{3,}")


class PendingStep(BaseModel):
    verb: str
    slug: str
    step: dict[str, Any]  # Step fields without `id` (assigned at build time)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:30] or "element"


def _paths(obs: Observation) -> dict[str, str]:
    return {f.frame.key: urlsplit(f.url).path for f in obs.frames}


def _headers(obs: Observation, frame_key: str) -> list[str]:
    seen: list[str] = []
    for el in obs.elements:
        tc = el.table_context
        if el.frame.key == frame_key and tc and tc.table_caption_or_header \
                and tc.table_caption_or_header not in seen:
            seen.append(tc.table_caption_or_header)
    return seen


class Recorder:
    def __init__(self, goal: DiscoveryGoal, profile: AppProfile, surface: Surface,
                 redactor: Redactor, logger: EventLogger, run: RunContext,
                 capabilities_root: Path = store.CAPABILITIES_DIR):
        self.goal = goal
        self.profile = profile
        self.surface = surface
        self.redactor = redactor
        self.log = logger
        self.run = run
        self.capabilities_root = Path(capabilities_root)
        self.inputs = goal.input_values()
        self.public_inputs = {i.name: str(i.value) for i in goal.inputs
                              if not i.sensitive and str(i.value).strip()}
        self.steps: list[PendingStep] = []
        self.visited: set[tuple[str, str]] = set()  # (frame key, path)
        self.success: Checkpoint | None = None

    # --- helpers --------------------------------------------------------------------------------

    def _warn(self, code: str, **data: Any) -> None:
        self.log.emit("warning", "agent", code=code, **data)

    def _templated(self, text: str) -> str:
        """Replace non-sensitive input literals in `text` with their {{inputs.x}} templates."""
        for name, value in sorted(self.public_inputs.items(), key=lambda kv: -len(kv[1])):
            text = text.replace(value, f"{{{{inputs.{name}}}}}")
        return text

    def _same_element(self, target: TargetSpec, el: ElementInfo) -> bool:
        try:
            handle, _ = self.surface.resolve(target.bind(self.inputs))
        except (TargetNotFound, ValueError):
            return False
        found = handle.element
        return found is not None and found.role == el.role and found.bbox == el.bbox

    def observe_pages(self, obs: Observation) -> None:
        self.visited |= set(_paths(obs).items())

    # --- targets --------------------------------------------------------------------------------

    def target_for(self, ref: str, el: ElementInfo, extract_text: str | None = None,
                   extracted: str | None = None, pattern: str | None = None,
                   obs: Observation | None = None) -> TargetSpec:
        """Durable, PII-safe TargetSpec for `ref` (call BEFORE acting: the ref must be live)."""
        spec = self.surface.describe(ref)
        strategies = list(spec.strategies)
        # (c) structural position keyed by a declared non-sensitive input.
        for i, s in enumerate(strategies):
            if isinstance(s, TableCell):
                name = next((n for n, v in self.public_inputs.items() if v == s.row_key), None)
                if name:
                    strategies[i] = s.model_copy(update={"row_key": f"{{{{inputs.{name}}}}}"})
        tc = el.table_context
        if tc and tc.column_header and not any(
                isinstance(s, TableCell) and s.row_key_input for s in strategies):
            for name in self.public_inputs:
                cand = TableCell(section_header=tc.table_caption_or_header,
                                 row_key=f"{{{{inputs.{name}}}}}", column_header=tc.column_header)
                probe = TargetSpec(frame=spec.frame, strategies=[cand], description="probe")
                if self._same_element(probe, el):
                    at = next((k for k, s in enumerate(strategies) if isinstance(s, CssPath)),
                              len(strategies))
                    strategies.insert(at, cand)
                    break
        # Strategies keyed on this run's input value (e.g. LabelAnchor(label="12345")) would
        # only match for this value; keep them only where they could be parameterized above.
        values = set(self.inputs.values())
        strategies = [s for s in strategies if isinstance(s, TableCell) or not any(
            isinstance(v, str) and v in values for v in s.model_dump().values())]
        description = self._templated(spec.description)
        if extract_text is not None:
            strategies = self._stable_for_extract(strategies, spec, el, extract_text,
                                                  extracted, pattern, obs)
            description = description.replace(" ".join(extract_text.split()), "<value>")
        fingerprint = spec.fingerprint
        if fingerprint is not None and extract_text is not None:
            # The fingerprint (diagnostics) must not carry this run's value either.
            name = fingerprint.name.replace(extracted, "<value>") if extracted \
                else fingerprint.name
            raw = " ".join(extract_text.split())
            name = name.replace(raw, "<value>") if raw else name
            fingerprint = fingerprint.model_copy(update={"name": name})
        if not strategies:
            raise SurfaceError("no PII-safe, stable strategy identifies this element")
        return spec.model_copy(update={"strategies": strategies, "description": description,
                                       "fingerprint": fingerprint})

    def _probe(self, spec: TargetSpec, strategy, el: ElementInfo) -> bool:
        return self._same_element(
            TargetSpec(frame=spec.frame, strategies=[strategy], description="probe"), el)

    def _stable_for_extract(self, strategies: list, spec: TargetSpec, el: ElementInfo,
                            raw: str, extracted: str | None, pattern: str | None,
                            obs: Observation | None) -> list:
        """Locators for a value that changes every run, in order: TableCell (business-label
        row key first), LabelAnchor (value next to its label), TextMatch on the stable part
        of the text; css_path last. Every candidate is validated on the live page."""
        raw_n = " ".join(raw.split())
        kept = [s for s in strategies if not raw_n
                or raw_n not in json.dumps(s.model_dump(), ensure_ascii=False)]
        semantic: list = []
        # 1. data grid: prefer a business label in the row (e.g. "Share Savings") over a code
        #    (e.g. suffix "00"); keep the code-keyed cell as the fallback.
        cells = [s for s in kept if isinstance(s, TableCell)]
        for cell in cells:
            label = self._business_row_label(el, cell, obs)
            if label and label != cell.row_key:
                cand = cell.model_copy(update={"row_key": label})
                if self._probe(spec, cand, el):
                    semantic.append(cand)
            semantic.append(cell)
        # 2. value next to its label cell ("BRANCH | 001 - MAIN OFFICE").
        tc = el.table_context
        label = tc.row_key if tc and tc.row_key and tc.row_key != el.name else None
        if label and not tc.column_header and not self.redactor.labels.kind_for(label):
            cand = LabelAnchor(label=label, role=el.role)
            if self._probe(spec, cand, el):
                semantic.append(cand)
        # 3. the stable part of the element's own text, e.g. "SUB-ACCOUNT OPENED — CONFIRMATION
        #    NO." once the value (the extract_pattern group) is cut out.
        stable = self._stable_text(raw_n, extracted, pattern)
        if stable:
            cand = TextMatch(text=stable, role=el.role, exact=False)
            if self._probe(spec, cand, el):
                semantic.append(cand)
        structural = [s for s in kept if isinstance(s, CssPath)]
        others = [s for s in kept if not isinstance(s, TableCell | CssPath)]
        return semantic + others + structural

    @staticmethod
    def _stable_text(raw: str, extracted: str | None, pattern: str | None) -> str:
        text = raw
        if pattern and (m := re.search(pattern, raw)):
            text = raw[:m.start(1)] + " " + raw[m.end(1):]
        elif extracted and extracted in raw:
            text = raw.replace(extracted, " ")
        else:
            text = re.sub(r"[$\d][\d,.\-]*", " ", raw)
        text = " ".join(text.split()).strip(" -—:")
        return text if len(text) >= 6 and re.search(r"[A-Za-z]{3}", text) else ""

    def _business_row_label(self, el: ElementInfo, cell: TableCell,
                            obs: Observation | None) -> str | None:
        """A non-numeric, non-sensitive cell text in the same row, e.g. "Share Savings"."""
        if obs is None or not re.fullmatch(r"[\d\W]+", cell.row_key or ""):
            return None
        tc = el.table_context
        for other in obs.elements:
            otc = other.table_context
            if (other is el or otc is None or other.frame.key != el.frame.key
                    or otc.row_key != tc.row_key
                    or otc.table_caption_or_header != tc.table_caption_or_header
                    or otc.column_header == tc.column_header):
                continue
            name = other.name
            if (re.search(r"[A-Za-z]{3}", name) and "[REDACTED" not in name
                    and self.redactor.redact_text(name) == name
                    and name not in self.inputs.values()):
                return name
        return None

    # --- recording ------------------------------------------------------------------------------

    def _value(self, call: ValidatedCall) -> str:
        value = call.value or ""
        if placeholders(value):
            return value
        name = next((n for n, v in self.inputs.items() if v == value and value), None)
        if name is not None:
            self._warn("parameterized_literal", input=name, tool=call.name)
            return f"{{{{inputs.{name}}}}}"
        if value:
            self._warn("literal_value_needs_review", tool=call.name, value=value)
        return value

    def record_action(self, call: ValidatedCall, target: TargetSpec, el: ElementInfo,
                      decision: PolicyDecision, pre: Observation, post: Observation,
                      changed: bool) -> None:
        if call.name in ("click", "press") and not changed:
            self.log.emit("dropped_step", "agent", tool=call.name, target=target.description,
                          reason="page unchanged and nothing was filled or extracted")
            return
        notes = None
        if call.name == "click":
            action: Any = Click(target=target)
        elif call.name == "press":
            action = Press(key=call.key or "Enter", target=target)
        else:
            value = self._value(call)
            if not placeholders(value) and value:
                notes = "REVIEW: literal value recorded as-is (not a declared input)"
            action = Fill(target=target, value=value) if call.name == "fill" \
                else Select(target=target, option=value)
        risk = decision.risk
        step = Step(id="pending", intent=self._templated(call.intent), action=action, risk=risk,
                    requires_approval=risk == "irreversible", approved=False,
                    expect=self._expect(pre, post), notes=notes)
        name = el.name if not el.name.startswith("[REDACTED") else (
            (el.table_context.column_header if el.table_context else None) or el.role)
        self.steps.append(PendingStep(
            verb=call.name, slug=_slug(name),
            step=step.model_dump(mode="json", exclude_none=True, exclude={"id"})))

    def record_extract(self, call: ValidatedCall, target: TargetSpec, el: ElementInfo) -> None:
        # Re-extracting an output replaces the earlier Extract (exactly one per output).
        self.steps = [p for p in self.steps
                      if not (p.verb == "extract"
                              and p.step["action"].get("output") == call.output)]
        step = Step(id="pending", intent=self._templated(call.intent),
                    action=Extract(target=target, output=call.output or ""), risk="safe")
        self.steps.append(PendingStep(verb="extract", slug=_slug(call.output or "value"),
                                      step=step.model_dump(mode="json", exclude_none=True,
                                                           exclude={"id"})))

    # --- checkpoints ----------------------------------------------------------------------------

    def _validated(self, conds: list[Condition]) -> list[Condition]:
        kept = []
        for c in conds:
            res = check_condition(self.surface, c, self.inputs)
            self.log.emit("checkpoint_generated", "agent", condition=res.condition, kept=res.ok,
                          observed=res.observed)
            if res.ok:
                kept.append(c)
        return kept

    def _expect(self, pre: Observation, post: Observation) -> Checkpoint | None:
        before, after = _paths(pre), _paths(post)
        changed = [k for k, p in after.items() if before.get(k) != p]
        if "_top" in changed:
            changed = ["_top"]
        conds: list[Condition] = []
        frames = {f.frame.key: f.frame for f in post.frames}
        for key in changed:
            segs = [("*" if _ID_LIKE.match(s) or s in self.inputs.values() else s)
                    for s in after[key].split("/")]
            frame = None if key == "_top" else frames[key]
            conds.append(UrlMatches(frame=frame, path_glob="/".join(segs) or "/"))
        for key in changed or [k for k in after if k != "_top"]:
            old = set(_headers(pre, key))
            for header in _headers(post, key):
                text = self._templated(header)
                if header in old or "[REDACTED" in header or _UNSTABLE.search(text) \
                        or self.redactor.redact_text(header) != header:
                    continue
                conds.append(TextPresent(frame=frames.get(key), text=text))
                break
        if not conds:
            conds = self._new_content(pre, post, frames)
        conds = self._validated(conds)
        return Checkpoint(description="postcondition recorded during discovery",
                          all_of=conds) if conds else None

    def _new_content(self, pre: Observation, post: Observation, frames: dict
                     ) -> list[Condition]:
        """URL and section header unchanged (e.g. a search on the same page): expect the
        newly appeared data-grid column headers, located by role + exact name."""
        old = {(e.frame.key, e.name) for e in pre.elements}
        headers = {(e.frame.key, e.table_context.column_header) for e in post.elements
                   if e.table_context and e.table_context.column_header}
        conds: list[Condition] = []
        for e in post.elements:
            if (e.frame.key, e.name) not in headers or (e.frame.key, e.name) in old:
                continue
            if (e.table_context and e.table_context.column_header) or "[REDACTED" in e.name \
                    or self.redactor.redact_text(e.name) != e.name \
                    or _UNSTABLE.search(self._templated(e.name)):
                continue
            target = TargetSpec(frame=e.frame, description=f"column header {e.name!r}",
                                strategies=[RoleName(role=e.role, name=e.name)])
            try:
                self.surface.resolve(target)
            except (TargetNotFound, SurfaceError):
                continue
            conds.append(TargetPresent(target=target))
            if len(conds) == 2:
                break
        return conds

    def finalize_success(self) -> None:
        """Success = the last navigation's expectations + every Extract target present.
        Validated against the live (final) page; call while it is still showing."""
        conds: list[Condition] = []
        for p in reversed(self.steps):
            exp = p.step.get("expect")
            if exp and any(c["kind"] == "url_matches" for c in exp["all_of"]):
                conds += Checkpoint.model_validate(exp).all_of
                break
        for p in self.steps:
            if p.verb == "extract":
                conds.append(TargetPresent(target=TargetSpec.model_validate(
                    p.step["action"]["target"])))
        conds = self._validated(conds)
        self.success = Checkpoint(description="final page shows the extracted values",
                                  all_of=conds) if conds else None

    # --- build ----------------------------------------------------------------------------------

    def _next_version(self) -> str:
        folder = self.capabilities_root / self.goal.capability_id
        versions = []
        for p in folder.glob("*.json") if folder.exists() else []:
            if ".rejected" not in p.name and re.fullmatch(r"\d+\.\d+\.\d+", p.stem):
                versions.append(tuple(int(x) for x in p.stem.split(".")))
        if not versions:
            return "1.0.0"
        major, minor, _ = max(versions)
        return f"{major}.{minor + 1}.0"

    def _contract(self) -> tuple[list[InputParam], list[OutputField]]:
        return self.goal.contract()  # validated before the run started; cannot drift

    def _outcomes(self) -> list[str]:
        """Profile outcomes whose scope_paths match a page this run visited."""
        visited = {p for _, p in self.visited}
        return [o.id for o in self.profile.outcomes if o.in_scope(visited)]

    def build(self, model_id: str, transcript_sha256: str | None
              ) -> tuple[Path | None, Path | None, list[str], Capability | None]:
        """Lint + save. Returns (artifact path, rejected path, lint errors, capability)."""
        version = self._next_version()
        inputs, outputs = self._contract()
        steps = []
        for n, p in enumerate(self.steps, 1):
            steps.append({**p.step, "id": f"s{n:02d}_{p.verb}_{p.slug}"})
        data = {
            "schema_version": "1.0", "id": self.goal.capability_id, "version": version,
            "title": self._templated(self.goal.title),
            # Never the goal: it holds this run's values. The redacted goal is in provenance.
            "description": self._templated(self.goal.description or self.goal.title),
            "status": "draft",
            "app": {"product": self.profile.product,
                    "product_versions": self.profile.product_versions,
                    "profile": self.profile.id, "surface": self.profile.surface},
            "inputs": [i.model_dump(mode="json", exclude_none=True) for i in inputs],
            "outputs": [o.model_dump(mode="json", exclude_none=True) for o in outputs],
            "use_profile_outcomes": self._outcomes(),
            "steps": steps,
            "success": self.success.model_dump(mode="json", exclude_none=True)
            if self.success else None,
            "provenance": Provenance(
                source="discovery", recorded_from_run_id=self.run.run_id,
                recorded_at=self.run.manifest.started_at, model_id=model_id,
                goal_redacted=self.redactor.redact_text(self.goal.goal),
                transcript_sha256=transcript_sha256, evidence_dir=str(self.run.dir),
                recorder_version=f"{RECORDER_VERSION} (cua {__version__})",
                git_commit=self.run.manifest.git_commit,
            ).model_dump(mode="json", exclude_none=True),
        }
        # Example values of inputs are kept only if the run redactor considers them safe.
        errors: list[str] = []
        # Hashes are excluded: a random hex digest can contain a 9+ digit run that the
        # ACCOUNT_NUMBER pattern would (harmlessly) match.
        checked = {**data, "provenance": {k: v for k, v in data["provenance"].items()
                                          if k not in ("transcript_sha256", "git_commit")}}
        if self.redactor.redact(checked) != checked:
            errors.append("[run_redactor] artifact contains values this run learned are "
                          "sensitive")
        if data["success"] is None:
            errors.append("[no_success_checkpoint] no verifiable success condition was found")
        cap = None
        if not errors:
            try:
                cap = Capability.model_validate(
                    data, context={"sensitive_labels": self.profile.sensitive_labels})
            except ValidationError as exc:
                errors += [str(e.get("msg", e)) for e in exc.errors()]
        if cap is not None:
            path = store.save(cap, root=self.capabilities_root)
            return path, None, [], cap
        rejected = self.capabilities_root / self.goal.capability_id / f"{version}.rejected.json"
        rejected.parent.mkdir(parents=True, exist_ok=True)
        payload = self.redactor.redact({"capability": data, "lint_errors": errors})
        rejected.write_text(store.canonical_json(json.loads(json.dumps(payload, default=str))),
                            encoding="utf-8")
        return None, rejected, errors, None
