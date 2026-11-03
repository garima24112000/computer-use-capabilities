"""Replay test helpers: real app + headless Chromium, no LLM, no network beyond localhost.

Artifact sets:
- "discovered": the artifacts in capabilities/ produced by real LLM discovery runs (tests skip
  with a clear message if they are missing);
- "fixture": the hand-written fixtures in tests/fixtures/capabilities/ (a second, independent set).
"""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cua.artifact import store
from cua.cli import app
from cua.replay.harness import FaultInjection
from cua.replay.session import resolve_artifact, run_replay

ROOT = Path(__file__).resolve().parents[2]
SETS = {
    "discovered": ROOT / "capabilities",
    "fixture": ROOT / "tests" / "fixtures" / "capabilities",
}
READ = "meridian.member.read_savings_balance"
OPEN = "meridian.subaccount.open"


def artifact_root(kind: str, cap_id: str) -> Path:
    root = SETS[kind]
    if not (root / cap_id).exists() or not list((root / cap_id).glob("*.json")):
        pytest.skip(f"no {kind} artifact for {cap_id} under {root} "
                    "(run `cua discover` to produce it)")
    path = resolve_artifact(cap_id, root)
    try:
        store.load(path)
    except ValidationError as exc:
        codes = sorted({e.split("]")[0].lstrip("[") for e in str(exc).splitlines()
                        if e.startswith("[")})
        pytest.skip(f"latest {kind} artifact {path.relative_to(ROOT)} fails current lint "
                    f"{codes}; re-run `cua discover` to produce a newer version")
    return root


def latest(kind: str, cap_id: str):
    return store.load(resolve_artifact(cap_id, artifact_root(kind, cap_id)))[0]


@pytest.fixture(autouse=True)
def _operator_secrets(monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_USER", "demo_operator")
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")


@pytest.fixture
def replay(tmp_path):
    """replay(base_url, cap_id, inputs, kind="discovered", inject=[...], **kw) -> result"""

    def run(base_url: str, cap_id: str, inputs: dict, *, kind: str = "discovered",
            root: Path | None = None, inject: list[FaultInjection] | None = None, **kw):
        caps = root or artifact_root(kind, cap_id)
        return run_replay(cap_id, inputs, base_url=base_url, runs_root=tmp_path / "runs",
                          capabilities_root=caps, injections=inject, **kw)

    return run


@pytest.fixture
def approved_open(tmp_path):
    """A copy of the artifact store where meridian.subaccount.open is approved via the CLI."""

    def make(kind: str) -> SimpleNamespace:
        src = artifact_root(kind, OPEN)
        dst = tmp_path / f"approved-{kind}"
        shutil.copytree(src, dst, dirs_exist_ok=True)
        path = resolve_artifact(OPEN, dst)
        out = CliRunner().invoke(app, ["artifact", "approve", str(path), "--reviewer", "qa"])
        assert out.exit_code == 0, out.output
        assert store.load(path)[0].is_approved()
        return SimpleNamespace(root=dst, path=path)

    return make


def run_dir(result) -> Path:
    return Path(result.evidence_dir)


def open_inputs(member: str, deposit: str = "250.00") -> dict:
    return {"member_id": member, "account_type": "Money Market", "initial_deposit": deposit,
            "nickname": ""}
