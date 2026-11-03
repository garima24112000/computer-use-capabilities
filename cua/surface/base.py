"""The Surface interface: the only seam discovery and replay use to perceive and act on a UI.

A Surface turns a live UI into typed Observations and executes actions against targets. Targets
are either an ephemeral `ref` from the latest observation (discovery: the model points at "e12")
or a durable TargetSpec (replay: ordered locator strategies recorded from a successful run).

Playwright web is only the first implementation. A desktop surface (Windows UI Automation or the
macOS AX tree) would produce the same ElementInfo roles/names from the accessibility tree; a
pure-vision surface (screenshot + coordinates) would populate elements from a detector and act by
bbox. Neither change touches discovery or replay, which must never import Playwright.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from cua.surface.locators import TargetSpec
from cua.surface.models import (
    ActionResult,
    ElementHandleRef,
    FrameRef,
    KeyContext,
    Observation,
    ResolveResult,
)

Target = str | TargetSpec
DialogDecision = Literal["accept", "dismiss"]
DialogPolicy = Callable[[str, str], DialogDecision]  # (dialog type, message) -> decision


def dismiss_all(dialog_type: str, message: str) -> DialogDecision:
    """Default dialog policy: dismiss everything."""
    return "dismiss"


class Surface(ABC):
    @abstractmethod
    def open(self, url: str) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def observe(self) -> Observation:
        """Enumerate frames/elements/text, assign fresh refs, drain pending dialog events."""

    @abstractmethod
    def describe(self, ref: str, description: str | None = None) -> TargetSpec:
        """Durable TargetSpec for an element of the current observation (strategies validated)."""

    @abstractmethod
    def resolve(self, target: TargetSpec) -> tuple[ElementHandleRef, ResolveResult]:
        """Try strategies in order; accept the first that matches exactly one element."""

    @abstractmethod
    def click(self, target: Target, *, settle: bool = True) -> ActionResult: ...

    @abstractmethod
    def fill(self, target: Target, value: str, *, settle: bool = True) -> ActionResult: ...

    @abstractmethod
    def select(self, target: Target, option_label: str, *, settle: bool = True
               ) -> ActionResult: ...

    @abstractmethod
    def press(self, key: str, target: Target | None = None, *, settle: bool = True
              ) -> ActionResult: ...

    def key_context(self, target: Target | None = None) -> KeyContext:
        """The element a key press would reach (target, or the focused element) and the submit
        control of its form. Surfaces that cannot tell return known=False."""
        return KeyContext(known=False, note=f"{type(self).__name__} has no key_context")

    @abstractmethod
    def read_text(self, target: Target) -> str: ...

    @abstractmethod
    def frame_text(self, frame: FrameRef | None = None) -> str:
        """Visible text of one frame, or of all frames joined by newlines when frame is None."""

    @abstractmethod
    def page_contains(self, text: str, frame: FrameRef | None = None) -> bool: ...

    @abstractmethod
    def screenshot(self, path: str | Path, mask_targets: list[TargetSpec] | None = None) -> Path:
        """Capture the viewport, painting over mask targets (redaction hooks in here later)."""

    @abstractmethod
    def current_urls(self) -> dict[str, str]:
        """Frame key -> URL for every attached frame."""

    @abstractmethod
    def wait_for_settle(self, timeout_ms: float | None = None) -> None:
        """Block until no navigation is pending, all frames are loaded and the DOM is quiet."""
