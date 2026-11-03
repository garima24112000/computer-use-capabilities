"""Policy configuration (configs/policy.yaml) and the typed decisions the guard produces."""

import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator

Risk = Literal["safe", "risky", "irreversible"]
Verdict = Literal["allow", "require_approval", "block"]
Action = Literal["click", "fill", "select", "press", "read", "navigate"]
Mode = Literal["discovery", "replay"]
ApprovalFlag = Literal["artifact_approved", "step_approved"]

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[2] / "configs" / "policy.yaml"


class RiskMatch(BaseModel):
    action: str | list[str] | None = None
    role: str | list[str] | None = None
    name_regex: str | None = None
    url_glob: str | None = None

    @field_validator("name_regex")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as exc:
                raise ValueError(f"invalid name_regex {v!r}: {exc}") from exc
        return v


class RiskRule(BaseModel):
    id: str
    match: RiskMatch
    risk: Risk
    reason: str | None = None


class ConditionalVerdict(BaseModel):
    """`verdict` only if every flag in `requires` is true on the PolicyContext, else `otherwise`."""

    verdict: Verdict
    requires: list[ApprovalFlag] = Field(default_factory=list)
    otherwise: Verdict


VerdictRule = Verdict | ConditionalVerdict


class PolicyConfig(BaseModel):
    version: int = 1
    allowed_origins: list[str]
    allowed_paths: list[str]
    blocked_paths: list[str] = Field(default_factory=list)
    allowed_actions: list[Action]
    risk_rules: list[RiskRule] = Field(default_factory=list)
    default_risk: dict[Action, Risk]
    mode_rules: dict[Mode, dict[Risk, VerdictRule]]
    sensitive_labels: list[str] = Field(default_factory=list)
    # App profile YAML (relative to the policy file) whose `sensitive_labels` are merged in.
    sensitive_labels_from: str | None = None

    @classmethod
    def load(cls, path: str | Path = DEFAULT_POLICY_PATH) -> "PolicyConfig":
        path = Path(path)
        data = yaml.safe_load(path.read_text())
        if ref := data.get("sensitive_labels_from"):
            profile = yaml.safe_load((path.parent / ref).read_text()) or {}
            merged = list(profile.get("sensitive_labels") or [])
            merged += [lab for lab in data.get("sensitive_labels") or [] if lab not in merged]
            data["sensitive_labels"] = merged
        return cls.model_validate(data)


class PolicyContext(BaseModel):
    mode: Mode
    current_url: str | None = None
    artifact_approved: bool = False
    step_approved: bool = False
    run_id: str | None = None
    step_id: str | None = None


class PolicyDecision(BaseModel):
    verdict: Verdict
    risk: Risk
    rule_id: str
    reason: str
