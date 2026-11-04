"""Agent-facing capability catalog: capabilities as tools a calling agent can list and invoke.

A tool is `Capability.to_tool_definition()` of one artifact version: by default the latest
APPROVED version of each capability id (drafts only with `include_drafts`). Invoking resolves the
tool name back to `id@version`, validates the arguments against the tool's `input_schema` and
the capability contract, and then runs the normal replay path (no LLM).
"""

import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cua.artifact import store
from cua.artifact.profile import PROFILES_DIR, AppProfile
from cua.artifact.schema import Capability, tool_name

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class CatalogEntry(BaseModel):
    tool: dict[str, Any]
    path: Path
    ref: str  # "id@version"
    status: str


class CatalogError(Exception):
    """Unknown tool or invalid arguments (a caller error: nothing was run)."""


def _profile(cap: Capability) -> AppProfile | None:
    path = PROFILES_DIR / f"{cap.app.profile}.yaml"
    return AppProfile.load(path) if path.exists() else None


def _versions(folder: Path) -> list[tuple[tuple[int, ...], Path]]:
    return sorted((tuple(map(int, m.groups())), p) for p in folder.glob("*.json")
                  if (m := _SEMVER.match(p.stem)))


def entries(root: Path = store.CAPABILITIES_DIR, include_drafts: bool = False
            ) -> list[CatalogEntry]:
    """One entry per capability id: its latest approved version, or with `include_drafts` its
    latest version of any status. Artifacts that fail validation are skipped."""
    out = []
    for folder in sorted(p for p in Path(root).iterdir() if p.is_dir()):
        for _, path in reversed(_versions(folder)):
            try:
                cap, _ = store.load(path)
            except Exception:
                continue
            if cap.status != "approved" and not include_drafts:
                continue
            out.append(CatalogEntry(tool=cap.to_tool_definition(_profile(cap)), path=path,
                                    ref=f"{cap.id}@{cap.version}", status=cap.status))
            break
    return out


def resolve(name: str, root: Path = store.CAPABILITIES_DIR, include_drafts: bool = False
            ) -> CatalogEntry:
    for entry in entries(root, include_drafts):
        if entry.tool["name"] == name:
            return entry
    known = [e.tool["name"] for e in entries(root, include_drafts)]
    raise CatalogError(f"unknown tool {name!r}; known: {known}")


def parse_args(text: str) -> dict[str, Any]:
    try:
        args = json.loads(text, parse_float=Decimal)  # keep "100.00" exactly
    except json.JSONDecodeError as exc:
        raise CatalogError(f"--args is not valid JSON ({exc})") from None
    if not isinstance(args, dict):
        raise CatalogError("--args must be a JSON object")
    return args


_JSON_TYPES = {"string": (str,), "number": (int, float, Decimal), "integer": (int,)}


def validate_args(entry: CatalogEntry, args: dict[str, Any]) -> dict[str, str]:
    """Check `args` against the tool's input_schema (types, required, no extras), then the
    capability contract (patterns, enums, bounds). Returns replay input values (strings)."""
    schema = entry.tool["input_schema"]
    props = schema["properties"]
    errors = [f"{k}: not an input of {entry.tool['name']}" for k in args if k not in props]
    errors += [f"{k}: required" for k in schema["required"] if k not in args]
    for k, v in args.items():
        want = _JSON_TYPES.get(props.get(k, {}).get("type", ""), ())
        if k in props and (isinstance(v, bool) or not isinstance(v, want)):
            errors.append(f"{k}: expected {props[k]['type']}, got {type(v).__name__}")
    if not errors:
        cap, _ = store.load(entry.path)
        errors += [f"{e.input}: {e.message}" for e in cap.validate_inputs(args)]
    if errors:
        raise CatalogError("invalid arguments: " + "; ".join(errors))
    return {k: str(v) for k, v in args.items()}


__all__ = ["CatalogEntry", "CatalogError", "entries", "parse_args", "resolve", "tool_name",
           "validate_args"]
