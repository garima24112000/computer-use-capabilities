"""Evidence capture: masked screenshots and redacted failure snapshots under runs/<run_id>/."""

import re
from pathlib import Path

from cua.obs.events import Actor, EventLogger
from cua.obs.run import RunContext
from cua.policy.guarded_surface import fail_closed_masks
from cua.policy.models import PolicyConfig
from cua.policy.redaction import Redactor, sensitive_mask_targets
from cua.surface.base import Surface


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40] or "step"


class EvidenceCapture:
    def __init__(self, surface: Surface, run: RunContext, redactor: Redactor,
                 policy: PolicyConfig, logger: EventLogger | None = None,
                 actor: Actor = "system"):
        self.surface = surface
        self.run = run
        self.redactor = redactor
        self.policy = policy
        self.logger = logger
        self.actor = actor
        self._n = 0

    def _screenshot(self, label: str, filename: str | None = None) -> Path:
        self._n += 1
        path = self.run.screenshots_dir / (filename or f"{self._n:03d}-{_slug(label)}.png")
        if getattr(self.surface, "masks_sensitive", False):
            self.surface.screenshot(path)  # GuardedSurface masks by itself
        else:
            raw = self.surface.observe()
            masks = sensitive_mask_targets(raw, self.policy.sensitive_labels)
            self.surface.screenshot(path, fail_closed_masks(self.surface, masks))
        return path

    def capture_step(self, step_label: str, step_id: str | None = None,
                     filename: str | None = None) -> Path:
        path = self._screenshot(step_label, filename)
        if self.logger:
            self.logger.emit("checkpoint", self.actor, step_id, label=step_label,
                             screenshot=self.run.rel(path))
        return path

    def capture_failure(self, reason: str, step_id: str | None = None) -> dict[str, str]:
        shot = self._screenshot(f"failure-{reason}")
        obs = self.redactor.redact_observation(self.surface.observe(),
                                               self.policy.sensitive_labels)
        snapshot = self.run.snapshots_dir / f"{self._n:03d}-failure.txt"
        snapshot.write_text(self.redactor.redact_text(obs.to_prompt_text()), encoding="utf-8")
        urls = self.redactor.redact(self.surface.current_urls())
        refs = {"screenshot": self.run.rel(shot), "snapshot": self.run.rel(snapshot)}
        if self.logger:
            self.logger.emit("error", self.actor, step_id, reason=reason, urls=urls, **refs)
        return refs
