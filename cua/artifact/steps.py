"""Steps: the recorded, replayable actions of a capability (also reused by app-profile subflows).

There are exactly six action types, and none of them carries free-form JavaScript. Every target
is a durable TargetSpec with ordered locator strategies (cua.surface.locators), and every typed
value is a ValueTemplate.
"""

from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from cua.artifact.conditions import Checkpoint
from cua.policy.models import Risk
from cua.surface.locators import TargetSpec
from cua.templating import validate_input_template, validate_template

# Literals plus {{inputs.x}} / {{secret:NAME}} (see cua.templating).
ValueTemplate = Annotated[str, AfterValidator(validate_template)]
STEP_ID = r"^[a-z][a-z0-9_]*$"
KEY = r"^(?:(?:Control|Shift|Alt|Meta)\+)*(?:[A-Za-z0-9]|[A-Z][A-Za-z0-9]+| )$"


class _Action(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Navigate(_Action):
    """Load a path on the app's origin (relative only, never a full URL; policy re-checks it)."""

    type: Literal["navigate"] = "navigate"
    url_path: Annotated[str, AfterValidator(validate_input_template)]

    @field_validator("url_path")
    @classmethod
    def _relative(cls, v: str) -> str:
        if not v.startswith("/") or "://" in v or v.startswith("//"):
            raise ValueError(f"url_path must be an app-relative path starting with '/': {v!r}")
        return v


class Click(_Action):
    type: Literal["click"] = "click"
    target: TargetSpec


class Fill(_Action):
    """Type a value. Credentials must be `{{secret:NAME}}`; caller data `{{inputs.x}}`."""

    type: Literal["fill"] = "fill"
    target: TargetSpec
    value: ValueTemplate


class Select(_Action):
    """Choose a <select> option by its visible label."""

    type: Literal["select"] = "select"
    target: TargetSpec
    option: Annotated[str, AfterValidator(validate_input_template)]


class Press(_Action):
    """Press a key (Playwright key name, e.g. "Enter", "Tab", "Control+A").

    With no target, the key goes to whatever has focus. Enter and Space are risk-classified at
    runtime by what they would trigger (see GuardedSurface.press).
    """

    type: Literal["press"] = "press"
    key: str = Field(pattern=KEY)
    target: TargetSpec | None = None


class Extract(_Action):
    """Read the target's text into the declared output `output` (normalized per OutputField)."""

    type: Literal["extract"] = "extract"
    target: TargetSpec
    output: str


Action = Annotated[Navigate | Click | Fill | Select | Press | Extract, Field(discriminator="type")]


def action_targets(action: Action) -> Iterator[tuple[str, TargetSpec]]:
    """(relative path, TargetSpec) for the action's target, if any."""
    target = getattr(action, "target", None)
    if target is not None:
        yield "target", target


RetryOn = Literal["settle_timeout", "target_not_found", "navigation_failed"]


class RetryPolicy(BaseModel):
    """Bounded, deterministic retry of one step: fixed backoff, only for the listed failures."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(1, ge=1, le=5)
    backoff_ms: int = Field(500, ge=0, le=30_000)
    on: list[RetryOn] = Field(default_factory=lambda: ["settle_timeout"])


class Step(BaseModel):
    """One replayable action plus why it is there, how risky it is and how to verify it.

    `id` is stable across versions, so tenant overrides and approvals refer to it. `risk` is what
    the policy said at record time, and GuardedSurface re-checks it live at replay time. When the
    two disagree, the stricter one wins. An irreversible step must have
    `requires_approval=True`. `approved` is set only by `cua artifact approve`, and it is
    excluded from the content hash.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=STEP_ID)
    intent: str
    action: Action
    risk: Risk
    requires_approval: bool = False
    approved: bool = False
    expect: Checkpoint | None = None
    settle_timeout_ms: int = Field(15_000, ge=0, le=120_000)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    notes: str | None = None
