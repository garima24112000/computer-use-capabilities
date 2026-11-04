"""ReplayResult: the typed contract returned to whoever invoked a capability (usually an agent).

Statuses
- success: every step ran and the success checkpoint holds; `outputs` are typed and normalized.
- business_outcome: the app returned a legitimate business result (e.g. MEMBER_NOT_FOUND).
- failed: the run could not complete; `failure` says why, where and whether to retry.
- escalated: a failure was handed to a human (Escalator) who resolved it out of band.

Failure categories and their fixed `retryable` flag. retryable=true means the SAME call may
succeed if simply retried later; false means the caller or a human must change something first.

| category                     | retry | meaning                                            |
|------------------------------|-------|----------------------------------------------------|
| input_validation             | no    | inputs violate the contract (all problems listed)  |
| artifact_invalid             | no    | artifact fails schema/lint or its tenant override  |
| artifact_not_approved        | no    | irreversible steps and not approved (or edited)    |
| policy_blocked               | no    | live policy blocked an action or navigation        |
| approval_denied              | no    | a required approval was refused                    |
| target_not_found             | no    | no locator matched and no known state explains it  |
| structural_only_on_risky     | no    | only a CSS path matched for a risky step           |
| checkpoint_failed            | no    | a postcondition did not hold (expected/observed)   |
| output_invalid               | no    | extracted text does not fit the output type        |
| app_error                    | yes   | the app showed a known hard error (0xC0FFEE)       |
| unknown_dialog               | no    | a JS dialog the profile does not know appeared     |
| unexpected_state             | no    | the app is in a state nothing explains             |
| settle_timeout               | yes   | the page did not settle in time (slow backend)     |
| session_expired_repeated     | yes   | the session expired again after one re-login       |
| session_expired_after_commit | no    | session lost after an irreversible step; a human   |
|                              |       | must check whether the commit happened first       |
| internal                     | no    | a bug in the engine (evidence still captured)      |

Recoveries (recorded in `recoveries`; the run still succeeded):
- interstitial: a profile interstitial was detected and its recover actions ran.
- dialog: a known JS dialog was answered per the profile.
- session_relogin: the session expired before any irreversible step; re-login, restart.
- settle_retry: the page did not settle within settle_timeout_ms and the step's retry policy
  allowed waiting again (no re-click, ever). Only for steps WITHOUT an `expect`.
- settle_absorbed_by_checkpoint (id "slow_settle"): the page did not settle in time, but the
  step's `expect` checkpoint then held within its own timeout; the detail gives the ms waited.
- human_intervention (id = intervention id): an operator took over the live session and
  resumed; detail = operator, number of human actions, and the resume rule that applied:
  success_checkpoint (only remaining Extract steps run), expect_holds (continue with the next
  step), retry_step (target resolves semantically; step retried once). Steps the human
  performed are reported with status "human". If the human did something policy classifies
  as irreversible, `irreversible_executed_by_human` is true.

Settle timeouts are never swallowed silently: with an `expect` the checkpoint decides (absorbed
→ recovery recorded; not holding → failed/settle_timeout); without one the retry policy decides
(→ settle_retry recoveries, or failed/settle_timeout when attempts run out).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class FailureCategory(StrEnum):
    input_validation = "input_validation"
    artifact_invalid = "artifact_invalid"
    artifact_not_approved = "artifact_not_approved"
    policy_blocked = "policy_blocked"
    approval_denied = "approval_denied"
    target_not_found = "target_not_found"
    structural_only_on_risky = "structural_only_on_risky"
    checkpoint_failed = "checkpoint_failed"
    output_invalid = "output_invalid"
    app_error = "app_error"
    unknown_dialog = "unknown_dialog"
    unexpected_state = "unexpected_state"
    settle_timeout = "settle_timeout"
    session_expired_repeated = "session_expired_repeated"
    session_expired_after_commit = "session_expired_after_commit"
    internal = "internal"


RETRYABLE: dict[FailureCategory, bool] = {
    FailureCategory.input_validation: False,
    FailureCategory.artifact_invalid: False,
    FailureCategory.artifact_not_approved: False,
    FailureCategory.policy_blocked: False,
    FailureCategory.approval_denied: False,
    FailureCategory.target_not_found: False,
    FailureCategory.structural_only_on_risky: False,
    FailureCategory.checkpoint_failed: False,
    FailureCategory.output_invalid: False,
    FailureCategory.app_error: True,
    FailureCategory.unknown_dialog: False,
    FailureCategory.unexpected_state: False,
    FailureCategory.settle_timeout: True,
    FailureCategory.session_expired_repeated: True,
    FailureCategory.session_expired_after_commit: False,
    FailureCategory.internal: False,
}

Status = Literal["success", "business_outcome", "failed", "escalated"]


class CapabilityRef(BaseModel):
    id: str
    version: str
    content_hash: str
    approved: bool


class Outcome(BaseModel):
    id: str
    code: str
    message: str


class Evidence(BaseModel):
    screenshot: str | None = None
    snapshot: str | None = None


class Failure(BaseModel):
    category: FailureCategory
    step_id: str | None = None
    retryable: bool
    expected: str
    observed: str
    resolve_attempts: list[dict[str, Any]] = Field(default_factory=list)
    evidence: Evidence = Field(default_factory=Evidence)
    escalation: dict[str, Any] | None = None  # the human's decision, if escalated


class Recovery(BaseModel):
    kind: Literal["interstitial", "dialog", "session_relogin", "settle_retry",
                  "settle_absorbed_by_checkpoint", "human_intervention"]
    id: str
    step_id: str | None = None
    detail: str = ""


class Drift(BaseModel):
    step_id: str
    winning_strategy_kind: str
    winning_index: int
    degraded: bool
    structural_only: bool


class StepRecord(BaseModel):
    step_id: str
    status: Literal["ok", "failed", "not_run", "human"]  # human: done by an operator
    duration_ms: float = 0.0


class ReplayResult(BaseModel):
    status: Status
    capability: CapabilityRef
    inputs: dict[str, Any] = Field(default_factory=dict)  # sensitive values redacted
    outputs: dict[str, Any] | None = None
    outcome: Outcome | None = None
    failure: Failure | None = None
    recoveries: list[Recovery] = Field(default_factory=list)
    drift: list[Drift] = Field(default_factory=list)
    steps: list[StepRecord] = Field(default_factory=list)
    irreversible_executed: list[str] = Field(default_factory=list)
    irreversible_executed_by_human: bool = False
    run_id: str
    evidence_dir: str
    started_at: datetime
    duration_ms: float = 0.0
