"""Control model for human-in-the-loop handoff: who drives the live browser, and when.

State machine (illegal transitions raise IllegalTransition):

    AUTOMATION     --escalate------------> AWAITING_HUMAN
    AWAITING_HUMAN --take_control--------> HUMAN
    HUMAN          --resume--------------> RESUMING
    RESUMING       --verified------------> AUTOMATION
    RESUMING       --verification_failed-> AWAITING_HUMAN
    AWAITING_HUMAN --handled|aborted|expired--> DONE
    HUMAN          --handled|aborted|expired--> DONE

The ControlLease names the single owner of the browser ("automation" or "human:<operator>").
Its epoch increments on every transfer. GuardedSurface calls `require_automation()` before
EVERY automation action, so one-owner-at-a-time is enforced at the seam, not by convention.
"""

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class ControlState(StrEnum):
    AUTOMATION = "AUTOMATION"
    AWAITING_HUMAN = "AWAITING_HUMAN"
    HUMAN = "HUMAN"
    RESUMING = "RESUMING"
    DONE = "DONE"


S = ControlState
TRANSITIONS: dict[tuple[ControlState, str], ControlState] = {
    (S.AUTOMATION, "escalate"): S.AWAITING_HUMAN,
    (S.AWAITING_HUMAN, "take_control"): S.HUMAN,
    (S.HUMAN, "resume"): S.RESUMING,
    (S.RESUMING, "verified"): S.AUTOMATION,
    (S.RESUMING, "verification_failed"): S.AWAITING_HUMAN,
    **{(state, event): S.DONE for state in (S.AWAITING_HUMAN, S.HUMAN)
       for event in ("handled", "aborted", "expired")},
}
AUTOMATION_OWNER = "automation"


class IllegalTransition(Exception):
    def __init__(self, state: ControlState, event: str):
        self.state, self.event = state, event
        allowed = sorted(e for (s, e) in TRANSITIONS if s == state)
        super().__init__(f"expected one of {allowed} in state {state}; observed: {event!r}")


class ControlNotHeld(Exception):
    """Automation tried to act while it does not hold the lease. Nothing was dispatched."""

    def __init__(self, owner: str, epoch: int, action: str):
        self.owner, self.epoch = owner, epoch
        super().__init__(f"expected: automation to hold the control lease for {action!r}; "
                         f"observed: lease held by {owner} (epoch {epoch})")


class ControlLease(BaseModel):
    owner: str = AUTOMATION_OWNER
    epoch: int = 0
    acquired_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Transition(BaseModel):
    from_state: ControlState
    to_state: ControlState
    event: str
    actor: str
    reason: str
    lease: ControlLease


Listener = Callable[[Transition], None]


class ControlSession:
    """Thread-safe state machine + lease for one run. Listeners see every transition (event log,
    store mirror for the console, in-page banner)."""

    def __init__(self, run_id: str, listeners: list[Listener] | None = None):
        self.run_id = run_id
        self.state = ControlState.AUTOMATION
        self.lease = ControlLease()
        self.listeners = list(listeners or [])
        self._lock = threading.RLock()

    def transition(self, event: str, actor: str, reason: str = "",
                   owner: str | None = None) -> Transition:
        """Apply `event`. Moving into HUMAN requires `owner` ("human:<operator>")."""
        with self._lock:
            to = TRANSITIONS.get((self.state, event))
            if to is None:
                raise IllegalTransition(self.state, event)
            new_owner = self.lease.owner
            if to == ControlState.HUMAN:
                if not owner or not owner.startswith("human:"):
                    raise ValueError("take_control needs owner='human:<operator>'")
                new_owner = owner
            elif to in (ControlState.AWAITING_HUMAN, ControlState.DONE):
                new_owner = "nobody"  # automation released it; nobody may act
            elif to in (ControlState.RESUMING, ControlState.AUTOMATION):
                new_owner = AUTOMATION_OWNER
            if new_owner != self.lease.owner:
                self.lease = ControlLease(owner=new_owner, epoch=self.lease.epoch + 1)
            t = Transition(from_state=self.state, to_state=to, event=event, actor=actor,
                           reason=reason, lease=self.lease)
            self.state = to
        for listener in self.listeners:
            listener(t)
        return t

    def require_automation(self, action: str) -> None:
        with self._lock:
            if self.lease.owner != AUTOMATION_OWNER:
                raise ControlNotHeld(self.lease.owner, self.lease.epoch, action)

    @property
    def human_active(self) -> bool:
        return self.state == ControlState.HUMAN


# --- in-page banner ---------------------------------------------------------------------------

BANNER_JS = """(text) => {
  const doc = document;
  let el = doc.querySelector('[data-cua-ignore="banner"]');
  if (!text) { if (el) el.remove(); return false; }
  const host = doc.body || doc.documentElement;
  if (!host || host.tagName === 'FRAMESET') return false;
  if (!el) {
    el = doc.createElement('div');
    el.setAttribute('data-cua-ignore', 'banner');
    el.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;' +
      'background:#b00020;color:#fff;font:bold 12px Verdana,sans-serif;padding:3px 8px;' +
      'opacity:0.92;pointer-events:none;text-align:center';
    host.appendChild(el);
  }
  el.textContent = text;
  return true;
}"""


def banner_text(state: ControlState, run_id: str, owner: str) -> str:
    if state == ControlState.HUMAN:
        operator = owner.removeprefix("human:")
        return (f"HUMAN IN CONTROL - {operator} - click Resume in the operator console when "
                "done")
    if state == ControlState.AWAITING_HUMAN:
        return f"WAITING FOR AN OPERATOR - run {run_id} - automation paused"
    if state == ControlState.DONE:
        return ""
    return f"AUTOMATION IN CONTROL - run {run_id}"
