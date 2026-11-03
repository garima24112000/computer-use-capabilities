"""`cua discover`: argument parsing, fail-fast validation, and an end-to-end scripted run."""

import contextlib
import json

import pytest
from typer.testing import CliRunner

import cua.discovery.session as session
from cua.cli import app, parse_input, parse_output
from cua.discovery.llm import BedrockConverseClient
from cua.discovery.models import ContractError, build_contract
from target_app import background


def test_parse_input_and_output_specs():
    assert parse_input(r"member_id=12345:string:^\d{5}$") == {
        "name": "member_id", "value": "12345", "type": "string", "required": True,
        "pattern": r"^\d{5}$"}
    assert parse_input(r"t=12:string:^\d+:\d+$")["pattern"] == r"^\d+:\d+$"  # ':' in a regex
    assert parse_input("account_type=Money Market:enum:Share Savings|Money Market|"
                       "Certificate 12mo") == {
        "name": "account_type", "value": "Money Market", "type": "enum", "required": True,
        "enum_values": ["Share Savings", "Money Market", "Certificate 12mo"]}
    opt = parse_input("nickname?=Rainy Day:string:^[A-Za-z0-9 ]{0,20}$")
    assert opt["name"] == "nickname" and opt["required"] is False
    assert parse_input("nickname?=:string")["value"] == ""
    assert parse_output("savings_balance:money:money_to_decimal") == {
        "name": "savings_balance", "type": "money", "normalize": "money_to_decimal"}


OPEN_ARGS = [
    "discover", "--goal", "Open a Money Market sub-account for member 22222 with 250.00",
    "--id", "meridian.subaccount.open", "--title", "Open a sub-account",
    "--input", r"member_id=22222:string:^\d{5}$",
    "--input", "initial_deposit=250.00:decimal",
    "--output", "confirmation_number:string:strip",
]


@pytest.fixture
def no_side_effects(monkeypatch, tmp_path):
    """Fail the test if the app, the LLM or a run dir would be started/created."""
    monkeypatch.setenv("BANK_OPERATOR_USER", "demo_operator")
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")

    def boom(*a, **k):
        raise AssertionError("side effect before validation finished")

    monkeypatch.setattr(background, "serve_in_thread", boom)
    monkeypatch.setattr(BedrockConverseClient, "from_env", classmethod(boom))
    monkeypatch.setattr(session, "run_discovery", boom)
    return tmp_path / "runs"


def test_run2_enum_without_values_fails_fast_with_exit_2(no_side_effects):
    """Regression: run 2 crashed in recorder.build() AFTER an approved SUBMIT."""
    args = [*OPEN_ARGS, "--input", "account_type=Money Market:enum",
            "--runs-dir", str(no_side_effects)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2, result.output
    assert "invalid capability contract" in result.output
    assert "account_type" in result.output and "enum_values is required" in result.output
    assert "Traceback" not in result.output
    assert not no_side_effects.exists()


@pytest.mark.parametrize("spec, message", [
    ("account_type=Gold:enum:Share Savings|Money Market", "expected one of"),
    ("member_id=abc:string:^\\d{5}$", "does not match"),
    ("initial_deposit=lots:decimal", "expected a decimal"),
    ("bad name=1:string", "input 'bad name'"),
    ("x=1:currency", "input 'x'"),
    ("novalue", "expected name=value:type"),
])
def test_invalid_inputs_fail_fast(no_side_effects, spec, message):
    result = CliRunner().invoke(app, [*OPEN_ARGS, "--input", spec,
                                      "--runs-dir", str(no_side_effects)])
    assert result.exit_code == 2 and message in result.output, result.output
    assert not no_side_effects.exists()


def test_invalid_outputs_and_id_fail_fast(no_side_effects):
    args = [*OPEN_ARGS, "--output", "balance:money:digits_only", "--output", "x:blob",
            "--id", "Not An Id", "--runs-dir", str(no_side_effects)]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 2
    for msg in ("normalize 'digits_only' does not apply to type 'money'", "output 'x'",
                "capability id 'Not An Id'"):
        assert msg in result.output


def test_fixed_run2_command_passes_validation():
    specs = [parse_input(r"member_id=22222:string:^\d{5}$"),
             parse_input("account_type=Money Market:enum:Share Savings|Money Market|"
                         "Certificate 12mo"),
             parse_input("initial_deposit=250.00:decimal"),
             parse_input("nickname?=:string:^[A-Za-z0-9 ]{0,20}$")]
    params, fields = build_contract("meridian.subaccount.open", specs,
                                    [parse_output("confirmation_number:string:strip")])
    assert [p.required for p in params] == [True, True, True, False]
    assert params[1].enum_values[1] == "Money Market" and params[3].example is None
    with pytest.raises(ContractError) as exc:
        build_contract("a.b", [parse_input("k=v:enum")], [])
    assert len(exc.value.problems) == 1


def test_unexpected_failure_is_readable_and_debug_shows_traceback(monkeypatch, tmp_path):
    monkeypatch.setenv("BANK_OPERATOR_USER", "demo_operator")
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")
    script = tmp_path / "s.json"
    script.write_text("[]")
    monkeypatch.setattr(background, "serve_in_thread",
                        lambda tenant: contextlib.nullcontext("http://127.0.0.1:9"))

    def explode(*a, **k):
        raise RuntimeError("browser exploded")

    monkeypatch.setattr(session, "run_discovery", explode)
    args = [*OPEN_ARGS, "--llm", f"scripted:{script}"]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1
    assert "error: discovery failed: RuntimeError: browser exploded" in result.output
    assert "Traceback" not in result.output
    debug = CliRunner().invoke(app, [*args, "--debug"])
    assert isinstance(debug.exception, RuntimeError)


SCRIPT = [
    [{"name": "click", "args": {"ref": "@button:MEMBER INQUIRY", "intent": "Open inquiry."}}],
    [{"name": "fill", "args": {"ref": "@textbox:MEMBER NO.", "value": "{{inputs.member_id}}",
                               "intent": "Enter the member number."}}],
    [{"name": "click", "args": {"ref": "@link:INQUIRE", "intent": "Run the inquiry."}}],
    [{"name": "click", "args": {"ref": "@link[col=NAME]", "intent": "Open the detail."}}],
    [{"name": "extract", "args": {"ref": "@cell[row=00,col=BALANCE]",
                                  "output": "savings_balance", "intent": "Read balance."}}],
    [{"name": "done", "args": {"summary": "Balance read."}}],
]


def test_cua_discover_with_scripted_llm(tmp_path, monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_USER", "demo_operator")
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")
    script = tmp_path / "script.json"
    script.write_text(json.dumps(SCRIPT))
    args = ["discover", "--goal", "Look up member 12345 and read their Share Savings balance",
            "--id", "meridian.member.read_savings_balance", "--title", "Read savings balance",
            "--input", r"member_id=12345:string:^\d{5}$",
            "--output", "savings_balance:money:money_to_decimal",
            "--llm", f"scripted:{script}", "--approve-mode", "deny",
            "--runs-dir", str(tmp_path / "runs"),
            "--capabilities-dir", str(tmp_path / "caps")]
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 0, result.output
    assert "status    : completed" in result.output
    assert (tmp_path / "caps" / "meridian.member.read_savings_balance" / "1.0.0.json").exists()

    bad = CliRunner().invoke(app, [*args[:-4], "--max-steps", "2",
                                   "--runs-dir", str(tmp_path / "runs"),
                                   "--capabilities-dir", str(tmp_path / "caps2")])
    assert bad.exit_code == 1 and "escalated" in bad.output
