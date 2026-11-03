"""RunContext: the per-run directory runs/<run_id>/ and its manifest.

Layout: manifest.json, events.jsonl, screenshots/, snapshots/, and optionally trace.zip.

Playwright traces contain the UNMASKED DOM (and typed values, including secrets), so tracing is
opt-in, the trace lives only under runs/ (gitignored), and it must never be copied into
committed evidence/ without manual review. It is excluded from the "grep the run dir" guarantee.
"""

import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel
from pydantic_core import to_jsonable_python

from cua.policy.redaction import Redactor

RunKind = Literal["discovery", "replay"]


class Traceable(Protocol):
    def start_trace(self, path: str | Path) -> None: ...
    def stop_trace(self) -> Path | None: ...


class RunManifest(BaseModel):
    run_id: str
    kind: RunKind
    started_at: datetime
    finished_at: datetime | None = None
    status: str | None = None
    capability: str | None = None
    goal: str | None = None
    tenant: str | None = None
    model_id: str | None = None
    git_commit: str | None = None
    trace_enabled: bool = False


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                             timeout=5, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def new_run_id() -> str:
    return f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:6]}"


class RunContext:
    def __init__(self, kind: RunKind, redactor: Redactor, *, root: str | Path = "runs",
                 capability: str | None = None, goal: str | None = None,
                 tenant: str | None = None, model_id: str | None = None, trace: bool = False):
        self.redactor = redactor
        self.run_id = new_run_id()
        self.dir = Path(root) / self.run_id
        self.screenshots_dir = self.dir / "screenshots"
        self.snapshots_dir = self.dir / "snapshots"
        for d in (self.screenshots_dir, self.snapshots_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self.trace_path = self.dir / "trace.zip"
        self._traced: Traceable | None = None
        self.manifest = RunManifest(
            run_id=self.run_id, kind=kind, started_at=datetime.now(UTC), capability=capability,
            goal=goal, tenant=tenant, model_id=model_id, git_commit=_git_commit(),
            trace_enabled=trace,
        )
        self._write_manifest()

    def _write_manifest(self) -> None:
        redacted = self.redactor.redact(self.manifest)
        (self.dir / "manifest.json").write_text(redacted.model_dump_json(indent=2))

    def write_json(self, name: str, data: object) -> Path:
        """Write a redacted JSON document (e.g. summary.json) into the run directory."""
        path = self.dir / name
        redacted = self.redactor.redact(to_jsonable_python(data))
        path.write_text(json.dumps(redacted, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                        encoding="utf-8")
        return path

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.dir))

    def start_trace(self, surface: Traceable) -> None:
        """Start Playwright tracing on `surface` if this run has tracing enabled."""
        if self.manifest.trace_enabled:
            surface.start_trace(self.trace_path)
            self._traced = surface

    def finish(self, status: str = "completed") -> None:
        if self._traced is not None:
            self._traced.stop_trace()
            self._traced = None
        self.manifest.finished_at = datetime.now(UTC)
        self.manifest.status = status
        self._write_manifest()
