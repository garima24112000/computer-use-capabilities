"""AppProfile: everything shared by all capabilities of one application, in reviewed YAML.

A capability says what to do. The profile says how to live with the app: how to sign on, which
interstitials and dialogs pop up and how to get past them, what an expired session or a system
error looks like, which business outcomes any screen can produce, and which labels mark PII.
Replay consults it between steps. The profile is also the single source of truth for
`sensitive_labels`, and configs/policy.yaml points at it via `sensitive_labels_from`.
"""

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cua.artifact.conditions import Checkpoint
from cua.artifact.contract import OutcomeSpec, SecretRef
from cua.artifact.lint import LintReport, lint_profile
from cua.artifact.steps import STEP_ID, Action, Step

PROFILES_DIR = Path(__file__).resolve().parents[2] / "configs" / "app_profiles"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Interstitial(_M):
    """A screen that can appear in place of the expected one, such as a maintenance notice.
    When `detect` holds, replay runs `recover` and then re-checks the step's expectation."""

    id: str = Field(pattern=STEP_ID)
    description: str
    detect: Checkpoint
    recover: list[Action] = Field(min_length=1)
    max_per_run: int = Field(3, ge=1)


class KnownDialog(_M):
    """A native JS dialog (alert/confirm) the app is known to raise, and how to answer it."""

    id: str = Field(pattern=STEP_ID)
    message_regex: str
    response: Literal["accept", "dismiss"]
    description: str = ""

    @field_validator("message_regex")
    @classmethod
    def _compiles(cls, v: str) -> str:
        try:
            re.compile(v)
        except re.error as exc:
            raise ValueError(f"invalid message_regex {v!r}: {exc}") from exc
        return v


class SessionExpiry(_M):
    """How an expired session looks. Replay re-runs the login subflow at most `max_per_run`
    times, then resumes at the step that was interrupted."""

    detect: Checkpoint
    recover: Literal["login_subflow"] = "login_subflow"
    max_per_run: int = Field(1, ge=0)


class ErrorSignature(_M):
    """A known failure screen. A hard error stops the run with this category, and the replay
    engine never retries past a hard error."""

    id: str = Field(pattern=STEP_ID)
    description: str
    detect: Checkpoint
    category: Literal["app_error", "permission", "unknown_state"]
    hard: bool = True


class AppProfile(_M):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    product: str
    product_versions: str
    surface: Literal["web"] = "web"
    description: str
    secrets: list[SecretRef] = Field(default_factory=list)
    login_subflow: list[Step] = Field(min_length=1)
    interstitials: list[Interstitial] = Field(default_factory=list)
    known_dialogs: list[KnownDialog] = Field(default_factory=list)
    session_expired: SessionExpiry
    error_signatures: list[ErrorSignature] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    sensitive_labels: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _lint(self) -> "AppProfile":
        report = self.lint()
        if not report.ok:
            raise ValueError(f"{len(report.errors)} lint error(s):\n"
                             + "\n".join(str(e) for e in report.errors))
        return self

    def lint(self) -> LintReport:
        return lint_profile(self)

    def missing_secrets(self, env: Mapping[str, str] | None = None) -> list[str]:
        """Names of the profile's secrets absent from the environment."""
        env = os.environ if env is None else env
        return [s.name for s in self.secrets if s.name not in env]

    def outcome(self, outcome_id: str) -> OutcomeSpec | None:
        return next((o for o in self.outcomes if o.id == outcome_id), None)

    def dialog_response(self, message: str) -> Literal["accept", "dismiss"] | None:
        for d in self.known_dialogs:
            if re.search(d.message_regex, message):
                return d.response
        return None

    @classmethod
    def load(cls, path: str | Path) -> "AppProfile":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))

    @classmethod
    def load_named(cls, name: str, profiles_dir: Path = PROFILES_DIR) -> "AppProfile":
        return cls.load(profiles_dir / f"{name}.yaml")
