"""Typed surface errors. Each carries what was expected and what was observed."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cua.surface.locators import TargetSpec
    from cua.surface.models import StrategyAttempt


class SurfaceError(Exception):
    """Base class for all surface failures."""


class TargetNotFound(SurfaceError):
    def __init__(self, target: "TargetSpec", attempts: list["StrategyAttempt"], note: str = ""):
        self.target = target
        self.attempts = attempts
        tried = "; ".join(f"{a.kind}={a.outcome}({a.match_count}){' ' + a.note if a.note else ''}"
                          for a in attempts)
        super().__init__(
            f"expected: {target.description!r} in frame {target.frame.key}; "
            f"observed: no strategy matched exactly one element [{tried}]"
            + (f"; {note}" if note else "")
        )


class ActionFailed(SurfaceError):
    def __init__(self, action: str, target: str, reason: str):
        self.action, self.target, self.reason = action, target, reason
        super().__init__(f"expected: {action} on {target!r} to succeed; observed: {reason}")


class SettleTimeout(SurfaceError):
    def __init__(self, frame_key: str, reason: str, timeout_ms: float):
        self.frame_key, self.reason, self.timeout_ms = frame_key, reason, timeout_ms
        super().__init__(
            f"expected: all frames settled within {timeout_ms:.0f}ms; "
            f"observed: frame {frame_key!r} still busy ({reason})"
        )


class NavigationFailed(SurfaceError):
    def __init__(self, url: str, reason: str):
        self.url, self.reason = url, reason
        super().__init__(f"expected: navigation to {url!r} to succeed; observed: {reason}")
