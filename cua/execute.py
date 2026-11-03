"""Deterministic execution of artifact actions and evaluation of checkpoints over a Surface.

Discovery uses this for the profile's login subflow, interstitial recovery and checkpoint
validation. Replay (step 6) will reuse it, so this module must never import the LLM / discovery
code. Everything goes through the Surface interface (in practice a GuardedSurface).
"""

import time
from collections.abc import Mapping
from fnmatch import fnmatch
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel

from cua.artifact.conditions import (
    Checkpoint,
    Condition,
    TargetAbsent,
    TargetPresent,
    TextAbsent,
    TextPresent,
    TitleContains,
    UrlMatches,
    describe_condition,
)
from cua.artifact.profile import AppProfile
from cua.artifact.steps import Action, Click, Extract, Fill, Navigate, Press, Select, Step
from cua.surface.base import DialogDecision, DialogPolicy, Surface
from cua.surface.errors import SurfaceError, TargetNotFound
from cua.surface.models import TOP_FRAME_KEY, ActionResult, FrameRef
from cua.templating import render

POLL_MS = 200


class ConditionResult(BaseModel):
    condition: str
    ok: bool
    observed: str = ""


class CheckpointResult(BaseModel):
    """Every condition's verdict, so a failure carries expected vs observed."""

    description: str
    ok: bool
    results: list[ConditionResult]

    def failures(self) -> str:
        return "; ".join(f"expected {r.condition}; observed: {r.observed}"
                         for r in self.results if not r.ok)


class StepFailed(Exception):
    def __init__(self, step_id: str, expected: str, observed: str):
        self.step_id, self.expected, self.observed = step_id, expected, observed
        super().__init__(f"step {step_id}: expected: {expected}; observed: {observed}")


def _norm(text: str) -> str:
    return " ".join(text.split())


def _frame_key(frame: FrameRef | None) -> str:
    return frame.key if frame is not None else TOP_FRAME_KEY


def _text(surface: Surface, frame: FrameRef | None) -> str:
    try:
        return _norm(surface.frame_text(frame))
    except SurfaceError:
        return ""


def check_condition(surface: Surface, c: Condition, inputs: Mapping[str, Any]
                    ) -> ConditionResult:
    desc = describe_condition(c)
    if isinstance(c, UrlMatches):
        url = surface.current_urls().get(_frame_key(c.frame))
        path = urlsplit(url).path if url else None
        return ConditionResult(condition=desc, ok=path is not None and fnmatch(path, c.path_glob),
                               observed=f"path {path}" if path else "frame not present")
    if isinstance(c, TextPresent | TextAbsent):
        text, wanted = _text(surface, c.frame), _norm(render(c.text, inputs))
        redactor = getattr(surface, "redactor", None)
        if redactor is not None:  # a GuardedSurface returns redacted text: compare like with like
            wanted = redactor.redact_text(wanted)
        present = text == wanted if getattr(c, "exact", False) else wanted in text
        ok = present if isinstance(c, TextPresent) else not present
        return ConditionResult(condition=desc, ok=ok, observed=f"frame text: {text[:160]!r}")
    if isinstance(c, TitleContains):
        obs = surface.observe()
        titles = [f.title for f in obs.frames
                  if c.frame is None or f.frame.key == c.frame.key]
        wanted = render(c.text, inputs)
        return ConditionResult(condition=desc, ok=any(wanted in t for t in titles),
                               observed=f"titles {titles}")
    if isinstance(c, TargetPresent | TargetAbsent):
        try:
            surface.resolve(c.target.bind(inputs))
            found, observed = True, "resolved"
        except TargetNotFound as exc:
            found, observed = False, str(exc)
        ok = found if isinstance(c, TargetPresent) else not found
        return ConditionResult(condition=desc, ok=ok, observed=observed[:200])
    raise TypeError(type(c).__name__)


def check_checkpoint(surface: Surface, cp: Checkpoint, inputs: Mapping[str, Any],
                     timeout_ms: float | None = None) -> CheckpointResult:
    """Poll until the checkpoint holds or the timeout (default: cp.timeout_ms) elapses."""
    deadline = time.monotonic() + (cp.timeout_ms if timeout_ms is None else timeout_ms) / 1000
    while True:
        all_res = [check_condition(surface, c, inputs) for c in cp.all_of]
        any_res = [check_condition(surface, c, inputs) for c in cp.any_of or []]
        ok = all(r.ok for r in all_res) and (not any_res or any(r.ok for r in any_res))
        if ok or time.monotonic() >= deadline:
            return CheckpointResult(description=cp.description, ok=ok, results=all_res + any_res)
        time.sleep(POLL_MS / 1000)


def run_action(surface: Surface, action: Action, inputs: Mapping[str, Any], base_url: str
               ) -> ActionResult | str | None:
    """Execute one action. Templates are rendered except secrets (GuardedSurface resolves them).
    Returns the ActionResult, or the text read for Extract."""
    target = getattr(action, "target", None)
    bound = target.bind(inputs) if target is not None else None
    if isinstance(action, Navigate):
        surface.open(base_url.rstrip("/") + render(action.url_path, inputs))
        return None
    if isinstance(action, Click):
        return surface.click(bound)
    if isinstance(action, Fill):
        return surface.fill(bound, render(action.value, inputs))
    if isinstance(action, Select):
        return surface.select(bound, render(action.option, inputs))
    if isinstance(action, Press):
        return surface.press(action.key, bound)
    if isinstance(action, Extract):
        return surface.read_text(bound)
    raise TypeError(type(action).__name__)


def run_steps(surface: Surface, steps: list[Step], inputs: Mapping[str, Any], base_url: str
              ) -> None:
    """Run steps in order, checking each `expect`. Raises StepFailed (expected vs observed)."""
    for step in steps:
        try:
            run_action(surface, step.action, inputs, base_url)
        except SurfaceError as exc:
            raise StepFailed(step.id, step.intent, str(exc)) from exc
        if step.expect is not None:
            res = check_checkpoint(surface, step.expect, inputs)
            if not res.ok:
                raise StepFailed(step.id, step.expect.description, res.failures())


def profile_dialog_policy(profile: AppProfile) -> DialogPolicy:
    """Accept/dismiss known dialogs as the profile says; dismiss anything unknown."""
    def policy(dialog_type: str, message: str) -> DialogDecision:
        return profile.dialog_response(message) or "dismiss"
    return policy
