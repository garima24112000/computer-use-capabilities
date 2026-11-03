"""The capability artifact: the product of discovery, and the only input replay needs.

A Capability describes one business operation in a UI that has no API, such as "read a member's
savings balance". It is:
- typed: inputs, outputs, secrets and business outcomes form a contract a calling agent can
  rely on;
- replayable without an LLM: an ordered list of Steps with durable multi-strategy locators,
  postconditions and bounded retries;
- reviewable: it is canonical JSON in git, `summary_markdown()` renders it for humans, and
  irreversible steps need an explicit approval;
- tamper-evident: `content_hash()` covers everything functional, and the approval records the
  hash it approved.

The submodules are conditions (the Checkpoint language), contract (inputs, outputs, secrets,
outcomes), steps (actions) and lint (the rules). This module re-exports all of them.
"""

import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, model_validator

from cua.artifact.conditions import (
    Checkpoint,
    Condition,
    InputTemplate,
    TargetAbsent,
    TargetPresent,
    TextAbsent,
    TextPresent,
    TitleContains,
    UrlMatches,
)
from cua.artifact.contract import InputError, InputParam, OutcomeSpec, OutputField, SecretRef
from cua.artifact.lint import LintReport, labels_from_context, lint_capability
from cua.artifact.steps import (
    Action,
    Click,
    Extract,
    Fill,
    Navigate,
    Press,
    RetryPolicy,
    Select,
    Step,
    ValueTemplate,
)

__all__ = [
    "Action", "AppRef", "Capability", "Checkpoint", "Click", "Condition", "Extract", "Fill",
    "InputError", "InputParam", "InputTemplate", "LintReport", "Navigate", "OutcomeSpec",
    "OutputField", "Preconditions", "Press", "Provenance", "RetryPolicy", "Review", "SecretRef",
    "Select", "Step", "TargetAbsent", "TargetPresent", "TextAbsent", "TextPresent",
    "TitleContains", "UrlMatches", "ValueTemplate",
]

SEMVER = r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?$"
CAPABILITY_ID = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"
# Fields that describe who/when/how-approved rather than what the capability does.
HASH_EXCLUDE: dict[str, Any] = {
    "status": True, "review": True, "provenance": True, "steps": {"__all__": {"approved"}},
}


class AppRef(BaseModel):
    """Which application (and versions) the capability was recorded against, and through which
    app profile (login, interstitials, shared outcomes) and surface it is replayed."""

    model_config = ConfigDict(extra="forbid")

    product: str = "meridian-core"
    product_versions: str = ">=4.2,<5"
    profile: str = "meridian-core"
    surface: Literal["web"] = "web"


class Preconditions(BaseModel):
    """Where the run starts. With session="login_subflow" replay first runs the profile's login
    subflow (secrets resolved at fill time), then the capability steps."""

    model_config = ConfigDict(extra="forbid")

    entry_path: str = "/login"
    session: Literal["login_subflow", "none"] = "login_subflow"


class Review(BaseModel):
    """Human approval record. Not part of the content hash."""

    model_config = ConfigDict(extra="forbid")

    reviewed_by: str | None = None
    approved_at: datetime | None = None
    notes: str | None = None
    approved_content_hash: str | None = None  # "sha256:<hex>" of what was approved


class Provenance(BaseModel):
    """Where this artifact came from. It stores hashes and pointers only: no transcript, no raw
    goal text, no screenshots. Evidence lives in (redacted) run directories."""

    model_config = ConfigDict(extra="forbid")

    source: Literal["discovery", "hand_written"]
    recorded_from_run_id: str | None = None
    recorded_at: datetime | None = None
    model_id: str | None = None
    goal_redacted: str
    transcript_sha256: str | None = Field(None, pattern=r"^[0-9a-f]{64}$")
    evidence_dir: str | None = None
    recorder_version: str
    git_commit: str | None = None


class Capability(BaseModel):
    """Root of a capability artifact (`capabilities/<id>/<version>.json`).

    `schema_version` is the version of this file format. `version` is the semver of this
    capability, with these rules:
    - MAJOR: the input/output contract changed (inputs, outputs, types, outcome codes). Calling
      agents pin MAJOR.
    - MINOR: steps or outcomes changed but the contract is the same (e.g. a new navigation path).
    - PATCH: locator, timeout or retry tweaks only, such as a strategy added after UI drift.

    The model validator runs the full lint (cua.artifact.lint) and reports every problem at
    once. Pass `context={"sensitive_labels": [...]}` to model_validate to add the app profile's
    labels; `store.load()` does that.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=CAPABILITY_ID)
    version: str = Field(pattern=SEMVER)
    title: str
    description: str
    status: Literal["draft", "approved", "deprecated"] = "draft"
    app: AppRef = Field(default_factory=AppRef)
    inputs: list[InputParam] = Field(default_factory=list)
    outputs: list[OutputField] = Field(default_factory=list)
    secrets: list[SecretRef] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    use_profile_outcomes: list[str] = Field(default_factory=list)
    preconditions: Preconditions = Field(default_factory=Preconditions)
    steps: list[Step] = Field(min_length=1)
    success: Checkpoint
    review: Review = Field(default_factory=Review)
    provenance: Provenance

    @model_validator(mode="after")
    def _lint(self, info: ValidationInfo) -> "Capability":
        report = self.lint(labels_from_context(info.context))
        if not report.ok:
            raise ValueError(f"{len(report.errors)} lint error(s):\n"
                             + "\n".join(str(e) for e in report.errors))
        return self

    # --- identity -------------------------------------------------------------------------------

    @property
    def major(self) -> int:
        return int(self.version.split(".")[0])

    def content_hash(self) -> str:
        """sha256 over canonical JSON of everything functional. status, review, provenance and
        per-step `approved` are excluded, so approving does not change the hash but any edit
        to steps, locators or the contract does."""
        data = self.model_dump(mode="json", exclude_none=True, exclude=HASH_EXCLUDE)
        blob = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()

    def is_approved(self) -> bool:
        """Approved, and not edited since: the replay-time `artifact_approved` flag."""
        return (self.status == "approved"
                and self.review.approved_content_hash == self.content_hash())

    def lint(self, sensitive_labels: list[str] | None = None) -> LintReport:
        return lint_capability(self, sensitive_labels)

    # --- contract -------------------------------------------------------------------------------

    def validate_inputs(self, values: dict[str, Any]) -> list[InputError]:
        declared = {p.name: p for p in self.inputs}
        errors = [InputError(input=k, code="unknown_input", message=f"unknown input {k!r}")
                  for k in values if k not in declared]
        for name, param in declared.items():
            errors += param.validate_value(values.get(name))
        return errors

    def to_tool_definition(self, profile: Any = None) -> dict[str, Any]:
        """How a calling agent sees this capability: a tool with JSON Schema in/out and the
        outcome codes it can return (pass the AppProfile to resolve inherited outcome codes)."""
        from cua.artifact.render import tool_definition
        return tool_definition(self, profile)

    def summary_markdown(self) -> str:
        """Human review rendering."""
        from cua.artifact.render import summary_markdown
        return summary_markdown(self)


def tool_name(capability_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", capability_id)[:64]
