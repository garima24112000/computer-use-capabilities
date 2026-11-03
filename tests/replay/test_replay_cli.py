"""`cua replay`: exit codes, --json, --repeat stability summary, harness injection banner."""

import contextlib
import json

import pytest
from typer.testing import CliRunner

from cua.cli import app
from target_app import background
from tests.replay.conftest import READ, SETS


@pytest.fixture
def use_app_a(app_a, monkeypatch):
    monkeypatch.setattr(background, "serve_in_thread",
                        lambda tenant: contextlib.nullcontext(app_a))
    return app_a


def cli(tmp_path, *args):
    return CliRunner().invoke(app, ["replay", *args, "--runs-dir", str(tmp_path / "runs"),
                                    "--capabilities-dir", str(SETS["fixture"])])


def test_exit_codes(use_app_a, tmp_path):
    ok = cli(tmp_path, READ, "--input", "member_id=12345")
    assert ok.exit_code == 0, ok.output
    assert ok.output.startswith("SUCCESS") and "savings_balance = 12345.67" in ok.output
    outcome = cli(tmp_path, READ, "--input", "member_id=99999")
    assert outcome.exit_code == 10 and "MEMBER_NOT_FOUND" in outcome.output
    failed = cli(tmp_path, READ, "--input", "member_id=abc")
    assert failed.exit_code == 20 and "input_validation" in failed.output
    assert cli(tmp_path, "no.such.capability").exit_code == 2
    assert cli(tmp_path, READ, "--input", "member_id").exit_code == 2
    assert cli(tmp_path, READ, "--inject", "{not json").exit_code == 2


def test_json_output_is_the_result_contract(use_app_a, tmp_path):
    out = cli(tmp_path, f"{READ}@1.0.0", "--input", "member_id=31415", "--json")
    assert out.exit_code == 0
    data = json.loads(out.output)
    assert data["status"] == "success" and data["outputs"] == {"savings_balance": "3141.59"}
    assert {"capability", "steps", "drift", "recoveries", "run_id", "evidence_dir"} <= set(data)


def test_repeat_prints_stability_summary(use_app_a, tmp_path):
    out = cli(tmp_path, READ, "--input", "member_id=12345", "--repeat", "3")
    assert out.exit_code == 0, out.output
    assert "STABILITY over 3 runs: success=3" in out.output
    assert "identical step sequence: yes" in out.output
    assert "identical outputs:       yes" in out.output and "p95" in out.output


def test_harness_injection_is_loud_and_recorded(use_app_a, tmp_path):
    out = cli(tmp_path, READ, "--input", "member_id=12345",
              "--inject-at", "s01_open_member_inquiry", '{"interstitial_count": 1}')
    assert out.exit_code == 0, out.output
    assert "FAULT INJECTED (harness)" in out.output
    assert "recovery interstitial:maintenance_notice at s01_open_member_inquiry" in out.output
    [run] = list((tmp_path / "runs").iterdir())
    manifest = json.loads((run / "manifest.json").read_text())
    assert manifest["harness_faults"][0]["at_step"] == "s01_open_member_inquiry"


def test_repeat_5_discovered_read_balance_is_deterministic(use_app_a, tmp_path):
    from tests.replay.conftest import artifact_root

    root = artifact_root("discovered", READ)
    out = CliRunner().invoke(app, ["replay", READ, "--input", "member_id=12345", "--repeat",
                                   "5", "--runs-dir", str(tmp_path / "runs"),
                                   "--capabilities-dir", str(root)])
    assert out.exit_code == 0, out.output
    assert "STABILITY over 5 runs: success=5" in out.output
    assert "identical step sequence: yes" in out.output
    assert "identical outputs:       yes" in out.output
    assert len(list((tmp_path / "runs").iterdir())) == 5
