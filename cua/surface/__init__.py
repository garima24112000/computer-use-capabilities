"""Perceive and act on a UI surface: the Surface interface, observers, actions and locators."""

from cua.surface.base import DialogPolicy, Surface, Target, dismiss_all
from cua.surface.errors import (
    ActionFailed,
    NavigationFailed,
    SettleTimeout,
    SurfaceError,
    TargetNotFound,
)
from cua.surface.locators import (
    CssPath,
    LabelAnchor,
    RoleName,
    Strategy,
    TableCell,
    TargetSpec,
    TextMatch,
)
from cua.surface.models import (
    ActionResult,
    DialogEvent,
    ElementInfo,
    FrameRef,
    Observation,
    ResolveResult,
)

__all__ = [
    "ActionFailed", "ActionResult", "CssPath", "DialogEvent", "DialogPolicy", "ElementInfo",
    "FrameRef", "LabelAnchor", "NavigationFailed", "Observation", "ResolveResult", "RoleName",
    "SettleTimeout", "Strategy", "Surface", "SurfaceError", "TableCell", "Target", "TargetNotFound",
    "TargetSpec", "TextMatch", "dismiss_all",
]
