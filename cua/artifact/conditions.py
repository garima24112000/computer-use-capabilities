"""Checkpoints: the condition language shared by every "is the UI in state X?" question.

The same small vocabulary is used by step postconditions (`Step.expect`), business outcome
detectors, the capability's success check, and app-profile detectors (interstitials, session
expiry, error signatures). The replay engine needs one evaluator, and a reviewer needs one set of
words. Conditions are declarative and side-effect free, and there is no free-form JavaScript.
"""

from collections.abc import Iterator
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from cua.surface.locators import TargetSpec
from cua.surface.models import FrameRef
from cua.templating import input_names, validate_input_template

# Text that is matched or shown: literals plus `{{inputs.x}}`, never secrets.
InputTemplate = Annotated[str, AfterValidator(validate_input_template)]


class _Cond(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UrlMatches(_Cond):
    """The frame's URL path (query ignored) matches an fnmatch glob, e.g. "/member/detail*"."""

    kind: Literal["url_matches"] = "url_matches"
    frame: FrameRef | None = None  # None = top document
    path_glob: str


class TextPresent(_Cond):
    """Visible text of the frame (any frame if None) contains `text`.

    The default is a case-sensitive substring match on whitespace-normalized text. With
    exact=True the frame's whole text must equal `text`. `text` may use `{{inputs.x}}`, but it
    must never contain sensitive values, and lint runs the Redactor over it.
    """

    kind: Literal["text_present"] = "text_present"
    frame: FrameRef | None = None
    text: InputTemplate
    exact: bool = False


class TextAbsent(_Cond):
    """Visible text of the frame (any frame if None) does not contain `text`."""

    kind: Literal["text_absent"] = "text_absent"
    frame: FrameRef | None = None
    text: InputTemplate


class TargetPresent(_Cond):
    """The target resolves to exactly one element (any strategy)."""

    kind: Literal["target_present"] = "target_present"
    target: TargetSpec


class TargetAbsent(_Cond):
    """The target resolves to no element."""

    kind: Literal["target_absent"] = "target_absent"
    target: TargetSpec


class TitleContains(_Cond):
    """The frame's document title contains `text`."""

    kind: Literal["title_contains"] = "title_contains"
    frame: FrameRef | None = None
    text: InputTemplate


Condition = Annotated[
    UrlMatches | TextPresent | TextAbsent | TargetPresent | TargetAbsent | TitleContains,
    Field(discriminator="kind"),
]


class Checkpoint(BaseModel):
    """A condition set that must become true within `timeout_ms`.

    Every `all_of` condition must hold, and when `any_of` is given at least one of it must hold
    too. The replay engine polls until the checkpoint holds or the timeout elapses, then reports
    which conditions failed (expected vs observed).
    """

    model_config = ConfigDict(extra="forbid")

    description: str
    all_of: list[Condition] = Field(default_factory=list)
    any_of: list[Condition] | None = None
    timeout_ms: int = Field(5000, ge=0, le=120_000)

    @model_validator(mode="after")
    def _not_empty(self) -> "Checkpoint":
        if not self.all_of and not self.any_of:
            raise ValueError("a checkpoint needs at least one condition in all_of or any_of")
        return self

    def conditions(self) -> Iterator[tuple[str, Condition]]:
        """(relative path, condition) for every condition."""
        for i, c in enumerate(self.all_of):
            yield f"all_of[{i}]", c
        for i, c in enumerate(self.any_of or []):
            yield f"any_of[{i}]", c

    def input_names(self) -> list[str]:
        names: list[str] = []
        for _, c in self.conditions():
            if isinstance(c, TargetPresent | TargetAbsent):
                names += c.target.input_names
            elif not isinstance(c, UrlMatches):
                names += input_names(c.text)
        return names


def describe_condition(c: Condition) -> str:
    """One-line human rendering, used by summary_markdown and failure messages."""
    def fr(frame: FrameRef | None) -> str:
        return frame.key if frame is not None else "any"

    if isinstance(c, UrlMatches):
        return f"url_matches[{c.frame.key if c.frame else '_top'}] `{c.path_glob}`"
    if isinstance(c, TextPresent):
        return f'text_present[{fr(c.frame)}] "{c.text}"' + (" (exact)" if c.exact else "")
    if isinstance(c, TextAbsent):
        return f'text_absent[{fr(c.frame)}] "{c.text}"'
    if isinstance(c, TitleContains):
        return f'title_contains[{fr(c.frame)}] "{c.text}"'
    kinds = " → ".join(s.kind for s in c.target.strategies)
    return f"{c.kind}[{c.target.frame.key}] {c.target.description} ({kinds})"
