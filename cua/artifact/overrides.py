"""Tenant overrides: one reviewed overlay per deployment, applied on top of a capability or profile.

The same product is deployed per tenant with small UI differences. For example tenant B labels
the member field "ACCT HOLDER #" and its search button "SEARCH". The fix is not a forked
artifact. A TenantOverride patches named steps (by stable step id) or profile paths, and the
patched result is re-validated and re-linted.

Overrides may only touch presentation: locators, texts and timeouts. `set_field` refuses fields
that carry risk or approval semantics, and nothing can patch the contract. Approval therefore
stays attached to the base artifact, and replay checks the base content hash.

Paths are dotted, with `[i]` indexes or `[id=value]` list selectors, relative to the step (or to
the profile with `profile:`), e.g. `action.target`, `expect.all_of[0].text`,
`profile:outcomes[id=member_not_found].detect.all_of[0].text`.
"""

import re
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.artifact.profile import AppProfile
from cua.artifact.schema import Capability

PROTECTED_FIELDS = {"risk", "requires_approval", "approved", "type", "id", "output"}
_SEGMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)((?:\[[^\]]+\])*)")
_INDEX = re.compile(r"\[([^\]]+)\]")
T = TypeVar("T", Capability, AppProfile)


class OverrideError(ValueError):
    pass


class AppliesTo(BaseModel):
    """A capability (with a MAJOR pin like "1.x") or a profile the override is written for."""

    model_config = ConfigDict(extra="forbid")

    capability: str | None = None
    versions: str = Field("*", pattern=r"^(\*|\d+\.x)$")
    profile: str | None = None

    @model_validator(mode="after")
    def _one(self) -> "AppliesTo":
        if (self.capability is None) == (self.profile is None):
            raise ValueError("applies_to entries name exactly one of capability / profile")
        return self

    def matches(self, obj: Capability | AppProfile) -> bool:
        if isinstance(obj, AppProfile):
            return self.profile == obj.id
        if self.capability != obj.id:
            return False
        return self.versions == "*" or int(self.versions.split(".")[0]) == obj.major


class Patch(BaseModel):
    """One edit. `at` is a step id (requires `capability`) or "profile:<path>".

    - replace_strategy: replace strategies[index] of the TargetSpec at `path`
    - prepend_strategy: insert `value` as the first strategy of the TargetSpec at `path`
    - replace_text: replace `old` with `value` in every string under `path` (path may be "")
    - set_field: set the field at `path` to `value`
    """

    model_config = ConfigDict(extra="forbid")

    at: str
    capability: str | None = None
    op: Literal["replace_strategy", "prepend_strategy", "replace_text", "set_field"]
    path: str = ""
    value: Any
    index: int = 0
    old: str | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "Patch":
        if self.at.startswith("profile:"):
            if self.capability is not None:
                raise ValueError("profile patches must not name a capability")
        elif self.capability is None:
            raise ValueError(f"step patch {self.at!r} must name its capability")
        if self.op == "replace_text" and not self.old:
            raise ValueError("replace_text needs `old`")
        if self.op == "set_field":
            last = re.sub(r"\[.*?\]", "", (self.full_path.split(".") or [""])[-1])
            if last in PROTECTED_FIELDS:
                raise ValueError(f"set_field may not change {last!r} (risk/approval/identity)")
        return self

    @property
    def full_path(self) -> str:
        return self.at.removeprefix("profile:") if self.at.startswith("profile:") else self.path


class TenantOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant: str
    reason: str
    applies_to: list[AppliesTo] = Field(min_length=1)
    patches: list[Patch] = Field(min_length=1)

    @classmethod
    def load(cls, path: str | Path) -> "TenantOverride":
        return cls.model_validate(yaml.safe_load(Path(path).read_text()))


# --- path navigation ---------------------------------------------------------------------------


def _steps(path: str) -> list[str | int | tuple[str, str]]:
    out: list[str | int | tuple[str, str]] = []
    for part in filter(None, path.split(".")):
        m = _SEGMENT.fullmatch(part)
        if m is None:
            raise OverrideError(f"bad path segment {part!r}")
        out.append(m.group(1))
        for idx in _INDEX.findall(m.group(2)):
            if idx.lstrip("-").isdigit():
                out.append(int(idx))
            elif "=" in idx:
                key, value = idx.split("=", 1)
                out.append((key, value))
            else:
                raise OverrideError(f"bad index [{idx}]")
    return out


def _child(node: Any, step: str | int | tuple[str, str], where: str) -> Any:
    try:
        if isinstance(step, tuple):
            return next(x for x in node if isinstance(x, dict) and str(x.get(step[0])) == step[1])
        return node[step]
    except (KeyError, IndexError, TypeError, StopIteration):
        raise OverrideError(f"path {where!r} does not exist") from None


def _get(root: Any, path: str) -> Any:
    node = root
    for step in _steps(path):
        node = _child(node, step, path)
    return node


def _set(root: Any, path: str, value: Any) -> None:
    steps = _steps(path)
    if not steps:
        raise OverrideError("set_field needs a path")
    parent = root
    for step in steps[:-1]:
        parent = _child(parent, step, path)
    last = steps[-1]
    if isinstance(last, tuple):
        raise OverrideError("cannot set a [key=value] selector; address a field inside it")
    try:
        if isinstance(last, int) or last in parent:
            parent[last] = value
        else:
            raise KeyError(last)
    except (KeyError, IndexError, TypeError):
        raise OverrideError(f"path {path!r} does not exist") from None


def _replace_text(node: Any, old: str, new: str) -> tuple[Any, int]:
    if isinstance(node, str):
        return node.replace(old, new), node.count(old)
    if isinstance(node, dict):
        total = 0
        for k, v in node.items():
            node[k], n = _replace_text(v, old, new)
            total += n
        return node, total
    if isinstance(node, list):
        total = 0
        for i, v in enumerate(node):
            node[i], n = _replace_text(v, old, new)
            total += n
        return node, total
    return node, 0


def _apply_patch(root: dict, patch: Patch) -> None:
    path = patch.full_path
    where = f"{patch.at} {patch.op} {patch.path}".strip()
    if patch.op == "set_field":
        _set(root, path, patch.value)
    elif patch.op == "replace_text":
        replaced, n = _replace_text(_get(root, path), patch.old or "", str(patch.value))
        if n == 0:
            raise OverrideError(f"{where}: text {patch.old!r} not found")
        if path:  # strings are immutable: write the result back
            _set(root, path, replaced)
    else:
        spec = _get(root, path)
        if not isinstance(spec, dict) or "strategies" not in spec:
            raise OverrideError(f"{where}: path does not point at a TargetSpec")
        if patch.op == "prepend_strategy":
            spec["strategies"].insert(0, patch.value)
        else:
            if not 0 <= patch.index < len(spec["strategies"]):
                raise OverrideError(f"{where}: no strategy at index {patch.index}")
            spec["strategies"][patch.index] = patch.value


def apply_override(obj: T, override: TenantOverride) -> T:
    """Pure: returns a new, re-validated (re-linted) object; `obj` is not modified."""
    if not any(a.matches(obj) for a in override.applies_to):
        name = obj.id + (f" v{obj.version}" if isinstance(obj, Capability) else "")
        raise OverrideError(f"override for tenant {override.tenant!r} does not apply to {name}")
    data = obj.model_dump(mode="json", exclude_none=True)
    for patch in override.patches:
        if isinstance(obj, AppProfile):
            if patch.at.startswith("profile:"):
                _apply_patch(data, patch)
            continue
        if patch.at.startswith("profile:") or patch.capability != obj.id:
            continue
        step = next((s for s in data["steps"] if s["id"] == patch.at), None)
        if step is None:
            raise OverrideError(f"step {patch.at!r} not found in {obj.id} v{obj.version}")
        _apply_patch(step, patch)
    if isinstance(obj, Capability):
        from cua.artifact.store import profile_labels
        return Capability.model_validate(
            data, context={"sensitive_labels": profile_labels(obj.app.profile)})
    return AppProfile.model_validate(data)
