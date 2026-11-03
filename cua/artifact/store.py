"""Serialization: canonical JSON on disk, so git diffs of artifacts are reviewable.

Layout: `capabilities/<id>/<version>.json`. A saved version is immutable in function. It can be
re-saved only when its content hash is unchanged, which happens when approval or review metadata
is updated. Any functional edit needs a new version.
"""

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cua.artifact.lint import LintReport
from cua.artifact.profile import PROFILES_DIR, AppProfile
from cua.artifact.schema import Capability

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPABILITIES_DIR = REPO_ROOT / "capabilities"
SCHEMAS_DIR = REPO_ROOT / "schemas"


class ArtifactExists(Exception):
    """Refusing to overwrite a saved version with functionally different content."""

    def __init__(self, path: Path, existing_hash: str, new_hash: str):
        self.path = path
        super().__init__(
            f"{path} already exists with different content; bump the version. expected: "
            f"content hash {existing_hash}; observed: {new_hash}")


def canonical_json(model: BaseModel | dict[str, Any]) -> str:
    """Sorted keys, 2-space indent, UTF-8 (no ASCII escaping), None fields omitted, newline."""
    data = model.model_dump(mode="json", exclude_none=True) if isinstance(model, BaseModel) \
        else model
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def artifact_path(cap: Capability, root: Path = CAPABILITIES_DIR) -> Path:
    return Path(root) / cap.id / f"{cap.version}.json"


def profile_labels(profile_name: str, profiles_dir: Path = PROFILES_DIR) -> list[str]:
    path = Path(profiles_dir) / f"{profile_name}.yaml"
    return AppProfile.load(path).sensitive_labels if path.exists() else []


def parse(data: dict[str, Any] | str, profiles_dir: Path = PROFILES_DIR
          ) -> tuple[Capability, LintReport]:
    """Validate (and lint, with the referenced profile's sensitive labels)."""
    if isinstance(data, str):
        data = json.loads(data)
    labels = profile_labels(data.get("app", {}).get("profile", "meridian-core"), profiles_dir)
    cap = Capability.model_validate(data, context={"sensitive_labels": labels})
    return cap, cap.lint(labels)


def load(path: str | Path, profiles_dir: Path = PROFILES_DIR) -> tuple[Capability, LintReport]:
    """Load + validate + lint. Raises pydantic.ValidationError listing every lint error."""
    return parse(Path(path).read_text(encoding="utf-8"), profiles_dir)


def save(cap: Capability, root: Path = CAPABILITIES_DIR, path: Path | None = None) -> Path:
    """Write canonical JSON. Overwrites only when the content hash is unchanged."""
    path = Path(path) if path is not None else artifact_path(cap, root)
    text = canonical_json(cap)
    if path.exists():
        existing, _ = load(path)
        if existing.content_hash() != cap.content_hash():
            raise ArtifactExists(path, existing.content_hash(), cap.content_hash())
        if path.read_text(encoding="utf-8") == text:
            return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def export_schemas(out_dir: Path = SCHEMAS_DIR) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in (("capability", Capability), ("app_profile", AppProfile)):
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://cua.local/schemas/{name}.schema.json"
        path = out_dir / f"{name}.schema.json"
        path.write_text(canonical_json(schema), encoding="utf-8")
        written.append(path)
    return written
