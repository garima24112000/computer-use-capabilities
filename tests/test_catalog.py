"""`cua catalog`: capabilities listed as agent tools and invoked by name (normal replay path)."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cua.cli import app

ROOT = Path(__file__).resolve().parents[1]
READ_TOOL = "meridian_member_read_savings_balance"
OPEN_TOOL = "meridian_subaccount_open"


@pytest.fixture(autouse=True)
def _secrets(monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_USER", "demo_operator")
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")


def cli(*args: str):
    return CliRunner().invoke(app, ["catalog", *args])


def test_list_shows_both_approved_capabilities_as_tools():
    out = cli("list", "--json")
    assert out.exit_code == 0, out.output
    tools = {t["name"]: t for t in json.loads(out.output)}
    assert set(tools) == {READ_TOOL, OPEN_TOOL}
    read = tools[READ_TOOL]
    assert read["capability"] == {"id": "meridian.member.read_savings_balance",
                                  "version": "1.1.0", "status": "approved"}
    assert read["input_schema"]["required"] == ["member_id"]
    assert "savings_balance" in read["output_schema"]["properties"]
    assert "MEMBER_NOT_FOUND" in read["outcomes"]
    assert "DEPOSIT_OUT_OF_RANGE" not in read["outcomes"]


def test_drafts_only_with_all(tmp_path):
    src = ROOT / "capabilities" / "meridian.member.read_savings_balance" / "1.1.0.json"
    folder = tmp_path / "meridian.member.read_savings_balance"
    folder.mkdir()
    data = json.loads(src.read_text())
    data["status"], data["review"] = "draft", {}
    (folder / "1.1.0.json").write_text(json.dumps(data))
    assert "no approved capabilities" in cli("list", "--capabilities-dir", str(tmp_path)).output
    out = cli("list", "--all", "--capabilities-dir", str(tmp_path))
    assert f"{READ_TOOL}  (meridian.member.read_savings_balance@1.1.0)  [DRAFT]" in out.output
    bad = cli("invoke", READ_TOOL, "--args", '{"member_id": "12345"}',
              "--capabilities-dir", str(tmp_path))
    assert bad.exit_code == 2 and "unknown tool" in bad.output  # drafts need --all


def test_invoke_by_name_runs_the_replay(tmp_path):
    out = cli("invoke", READ_TOOL, "--args", '{"member_id": "12345"}', "--json",
              "--runs-dir", str(tmp_path / "runs"))
    assert out.exit_code == 0, out.output
    result = json.loads(out.stdout[out.stdout.index("{"):])
    assert result["status"] == "success"
    assert result["outputs"] == {"savings_balance": "12345.67"}
    assert result["capability"]["version"] == "1.1.0"


@pytest.mark.parametrize("args, message", [
    ('{"member_id": "abc"}', "does not match"),
    ('{"member_id": 12345}', "expected string"),
    ('{}', "member_id: required"),
    ('{"member_id": "12345", "extra": 1}', "extra: not an input"),
    ('["12345"]', "must be a JSON object"),
    ('{member_id}', "not valid JSON"),
])
def test_invalid_args_exit_2_before_any_browser(tmp_path, args, message):
    runs = tmp_path / "runs"
    out = cli("invoke", READ_TOOL, "--args", args, "--runs-dir", str(runs))
    assert out.exit_code == 2
    assert message in out.output
    assert not runs.exists()  # nothing ran


def test_deposit_bounds_and_number_type(tmp_path):
    runs = tmp_path / "runs"
    args = {"member_id": "31415", "account_type": "Share Savings", "initial_deposit": 20000}
    out = cli("invoke", OPEN_TOOL, "--args", json.dumps(args), "--runs-dir", str(runs))
    assert out.exit_code == 2 and "must be <= 10000" in out.output
    args["initial_deposit"] = "100.00"  # the schema says number
    out = cli("invoke", OPEN_TOOL, "--args", json.dumps(args), "--runs-dir", str(runs))
    assert out.exit_code == 2 and "expected number" in out.output
    assert not runs.exists()


def test_unknown_tool_exit_2(tmp_path):
    out = cli("invoke", "meridian_does_not_exist", "--args", "{}")
    assert out.exit_code == 2
    assert "unknown tool" in out.output and READ_TOOL in out.output


def test_catalog_does_not_import_the_llm():
    code = ("import sys, cua.catalog, cua.replay.session; "
            "print(any(m.startswith(('cua.discovery', 'boto3')) for m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT)
    assert out.stdout.strip() == "False", out.stderr
