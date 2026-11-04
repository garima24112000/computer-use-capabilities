"""Structured, append-only JSONL event log. Every event is redacted before it is written."""

import threading
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic_core import to_jsonable_python

from cua.obs.run import RunContext
from cua.policy.redaction import Redactor

EventType = Literal[
    "run_started", "observation", "llm_decision", "action", "policy_decision", "network_blocked",
    "approval_requested", "approval_result", "dialog", "recovery", "checkpoint", "outcome",
    "escalation", "human_action", "error", "run_finished",
    "checkpoint_generated", "dropped_step", "warning",
    "replay_started", "step_started", "resolve", "sweep_result", "failure", "replay_finished",
    "harness_fault", "control_transition", "intervention", "session_check",
]
Actor = Literal["agent", "replay", "human", "system"]


class Event(BaseModel):
    ts: datetime
    run_id: str
    seq: int
    type: EventType
    actor: Actor
    step_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class EventLogger:
    """Writes runs/<run_id>/events.jsonl. There is no unredacted write path."""

    def __init__(self, run: RunContext, redactor: Redactor):
        self.run = run
        self.redactor = redactor
        self._seq = 0
        self._lock = threading.Lock()
        self._fh = open(run.events_path, "a", encoding="utf-8")  # noqa: SIM115

    def emit(self, type: EventType, actor: Actor = "system", step_id: str | None = None,
             **data: Any) -> Event:
        with self._lock:
            self._seq += 1
            event = Event(ts=datetime.now(UTC), run_id=self.run.run_id, seq=self._seq, type=type,
                          actor=actor, step_id=step_id, data=to_jsonable_python(data))
            event = self.redactor.redact(event)
            self._fh.write(event.model_dump_json() + "\n")
            self._fh.flush()
            return event

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()
