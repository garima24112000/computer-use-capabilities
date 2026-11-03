"""TEST HARNESS ONLY: fault injection into the target app's control plane.

Faults are POSTed to /__faults with plain httpx, never through the agent's GuardedSurface (whose
network filter blocks that path). Every injection is recorded in the run manifest and the event
log, so a run with injected faults can never be mistaken for a clean run.
"""

from typing import Any

import httpx
from pydantic import BaseModel

from cua.obs.events import EventLogger
from cua.obs.run import RunContext


class FaultInjection(BaseModel):
    faults: dict[str, Any]
    at_step: str | None = None  # None = before the run starts


def inject(base_url: str, injection: FaultInjection, run: RunContext,
           logger: EventLogger | None) -> None:
    httpx.post(f"{base_url.rstrip('/')}/__faults", json=injection.faults,
               timeout=10).raise_for_status()
    run.manifest.harness_faults.append(injection.model_dump())
    run._write_manifest()
    if logger is not None:
        logger.emit("harness_fault", "system", injection.at_step, faults=injection.faults)
