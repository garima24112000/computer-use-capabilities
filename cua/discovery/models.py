"""Typed inputs and outputs of a discovery run."""

import re
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from cua.artifact.contract import InputParam, InputType, Normalize, OutputField, OutputType
from cua.artifact.schema import CAPABILITY_ID
from cua.discovery.llm import Usage

# Which normalizers make sense for which output types (fail fast on nonsense combinations).
NORMALIZERS: dict[str, set[str]] = {
    "string": {"none", "strip", "digits_only"},
    "integer": {"none", "strip", "digits_only"},
    "decimal": {"none", "strip", "money_to_decimal"},
    "money": {"none", "strip", "money_to_decimal"},
    "date": {"none", "strip"},
}


class GoalInput(BaseModel):
    """A declared input and the concrete value used for this discovery run."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: InputType = "string"
    value: Any
    pattern: str | None = None
    enum_values: list[str] | None = None
    min: Decimal | None = None
    max: Decimal | None = None
    sensitive: bool = False
    required: bool = True
    description: str | None = None

    def given(self) -> Any:
        """The value as provided, or None for an optional input given as empty."""
        return None if (not self.required and str(self.value) == "") else self.value


class GoalOutput(BaseModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    type: OutputType = "string"
    normalize: Normalize = "none"
    extract_pattern: str | None = None
    sensitive: bool = False
    description: str | None = None


class DiscoveryGoal(BaseModel):
    goal: str
    capability_id: str
    title: str
    inputs: list[GoalInput] = Field(default_factory=list)
    outputs: list[GoalOutput] = Field(default_factory=list)
    tenant: str = "a"
    base_url: str

    def input_values(self) -> dict[str, str]:
        return {i.name: "" if i.given() is None else str(i.value) for i in self.inputs}

    def contract(self) -> tuple[list[InputParam], list[OutputField]]:
        return build_contract(self.capability_id, self.inputs, self.outputs)


class ContractError(ValueError):
    """The declared inputs/outputs are invalid. Raised before anything has side effects."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("invalid capability contract:\n" + "\n".join(f"  - {p}"
                                                                       for p in problems))


def _first_error(exc: ValidationError) -> str:
    return "; ".join(e["msg"].removeprefix("Value error, ") for e in exc.errors())


def build_contract(capability_id: str, inputs: list[GoalInput | dict],
                   outputs: list[GoalOutput | dict]
                   ) -> tuple[list[InputParam], list[OutputField]]:
    """Build and fully validate the contract: InputParam/OutputField objects, each given value
    against its own InputParam, and output type/normalizer compatibility. All problems at once.
    """
    problems: list[str] = []
    if not re.match(CAPABILITY_ID, capability_id):
        problems.append(f"capability id {capability_id!r} must be dotted lowercase, "
                        "e.g. meridian.member.read_savings_balance")
    params: list[InputParam] = []
    for raw in inputs:
        try:
            gi = raw if isinstance(raw, GoalInput) else GoalInput.model_validate(raw)
        except ValidationError as exc:
            problems.append(f"input {dict(raw).get('name', '?')!r}: {_first_error(exc)}")
            continue
        try:
            param = InputParam(
                name=gi.name, type=gi.type, required=gi.required,
                description=gi.description or gi.name.replace("_", " "),
                pattern=gi.pattern, enum_values=gi.enum_values, min=gi.min, max=gi.max,
                sensitive=gi.sensitive)
        except ValidationError as exc:
            problems.append(f"input {gi.name!r}: {_first_error(exc)}")
            continue
        errors = param.validate_value(gi.given())
        if errors:
            problems += [f"input {gi.name!r}: {e.message}" for e in errors]
            continue
        if not gi.sensitive and gi.given() is not None:
            param = param.model_copy(update={"example": str(gi.value)})
        params.append(param)
    fields: list[OutputField] = []
    for raw in outputs:
        try:
            go = raw if isinstance(raw, GoalOutput) else GoalOutput.model_validate(raw)
            field = OutputField(
                name=go.name, type=go.type, normalize=go.normalize,
                description=go.description or go.name.replace("_", " "),
                extract_pattern=go.extract_pattern, sensitive=go.sensitive)
        except ValidationError as exc:
            problems.append(f"output {dict(raw).get('name', '?')!r}: {_first_error(exc)}")
            continue
        if go.normalize not in NORMALIZERS[go.type]:
            problems.append(f"output {go.name!r}: normalize {go.normalize!r} does not apply to "
                            f"type {go.type!r} (use one of {sorted(NORMALIZERS[go.type])})")
            continue
        fields.append(field)
    for kind, names in (("input", [p.name for p in params]), ("output", [f.name for f in fields])):
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            problems.append(f"duplicate {kind} names: {dupes}")
    if problems:
        raise ContractError(problems)
    return params, fields


class DiscoveryConfig(BaseModel):
    max_steps: int = Field(25, ge=1)
    max_invalid_calls: int = Field(3, ge=1)  # consecutive
    no_progress_limit: int = Field(3, ge=1)  # consecutive actions with an unchanged page
    wall_timeout_s: float = Field(600, gt=0)
    screenshots: bool = True


Status = Literal["completed", "business_outcome", "escalated", "error"]


class DiscoveryResult(BaseModel):
    status: Status
    run_id: str
    run_dir: str
    turns: int = 0
    actions_executed: int = 0
    recorded_steps: int = 0
    outputs: dict[str, str | None] = Field(default_factory=dict)  # in memory only (raw)
    outcome: str | None = None  # business outcome code
    reason: str | None = None  # escalation / error reason
    artifact_path: str | None = None
    rejected_path: str | None = None
    lint_errors: list[str] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    wall_s: float = 0.0
    executed_actions: list["ExecutedAction"] = Field(default_factory=list)
    irreversible_executed: bool = False
    irreversible_steps_executed: list[str] = Field(default_factory=list)


class ExecutedAction(BaseModel):
    """An action that reached the application (side effects possible)."""

    turn: str
    action: str  # redacted summary, e.g. 'click button "SUBMIT"'
    risk: str
    verdict: str
    rule_id: str
    ok: bool = True  # False: dispatched but failed afterwards (it may still have taken effect)


DiscoveryResult.model_rebuild()
