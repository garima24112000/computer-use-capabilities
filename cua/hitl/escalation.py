"""The escalation seam shared by discovery and replay (step 7 plugs the HITL operator in here).

Neutral module: replay must not import cua.discovery, so the protocol lives here.
"""

from typing import Literal, Protocol

from pydantic import BaseModel, Field


class EscalationRequest(BaseModel):
    reason: str
    raised_by: Literal["agent", "system", "replay"]
    run_id: str
    turn: int = 0  # discovery turn (0 for replay)
    step_id: str | None = None  # replay step
    category: str | None = None  # replay failure category
    evidence: dict[str, str] = Field(default_factory=dict)
    # Context for a human (all redacted by the caller).
    kind: str | None = None  # discovery_stuck | replay_failure | commit_uncertain
    capability: str | None = None  # "id@version"
    goal: str | None = None
    step_intent: str | None = None
    expected: str | None = None
    observed: str | None = None


class EscalationDecision(BaseModel):
    """stop: give up. resumed: a human fixed the UI state; continue. handled: a human
    completed or resolved the task out of band (`resolution` says how)."""

    action: Literal["stop", "resumed", "handled"] = "stop"
    note: str | None = None
    resolution: str | None = None
    # Filled by a HITL escalator.
    intervention_id: str | None = None
    operator: str | None = None
    human_action_count: int = 0
    human_action_summaries: list[str] = Field(default_factory=list)
    human_irreversible: bool = False

    @property
    def resume(self) -> bool:
        return self.action == "resumed"


class Escalator(Protocol):
    def escalate(self, request: EscalationRequest) -> EscalationDecision: ...


class StopEscalator:
    """Default: the caller has already logged the escalation with evidence; stop."""

    def escalate(self, request: EscalationRequest) -> EscalationDecision:
        return EscalationDecision(action="stop")
