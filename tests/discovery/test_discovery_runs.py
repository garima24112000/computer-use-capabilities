"""End-to-end discovery runs against the real app with scripted LLM decisions."""

import json

import httpx

from cua.artifact import store
from cua.policy.guard import AutoApproveForTests, DenyAllApprover
from cua.surface.locators import TableCell
from tests.artifact.conftest import READ_BALANCE
from tests.discovery.conftest import T, click, done, fill, read_balance_script, select
from tests.surface.helpers import set_faults


def events(run_dir) -> list[dict]:
    return [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]


def test_happy_path_records_a_lint_clean_capability_like_the_fixture(app_a, discover):
    d = discover(app_a, read_balance_script())
    r = d.result
    assert r.status == "completed", r.reason
    assert r.outputs == {"savings_balance": "12345.67"}
    assert r.artifact_path.endswith("meridian.member.read_savings_balance/1.0.0.json")
    cap = d.cap
    assert cap.status == "draft" and cap.lint(store.profile_labels("meridian-core")).ok
    fixture, _ = store.load(READ_BALANCE)
    assert [s.action.type for s in cap.steps] == [s.action.type for s in fixture.steps]
    assert [s.id for s in cap.steps] == [
        "s01_click_member_inquiry", "s02_fill_member_no", "s03_click_inquire", "s04_click_name",
        "s05_extract_savings_balance"]
    assert cap.steps[1].action.value == "{{inputs.member_id}}"
    name_link = cap.steps[3].action.target
    assert name_link.strategies[0] == TableCell(
        section_header="MEMBER INQUIRY", row_key="{{inputs.member_id}}", column_header="NAME")
    assert "12345" not in name_link.model_dump_json()  # value-keyed strategies were dropped
    assert [o.name for o in cap.outputs] == ["savings_balance"]
    assert cap.outputs[0].normalize == "money_to_decimal"
    assert cap.inputs[0].pattern == r"^\d{5}$" and cap.inputs[0].example == "12345"
    assert any(c.kind == "target_present" for c in cap.success.all_of)
    assert cap.provenance.source == "discovery" and cap.provenance.recorded_from_run_id == \
        r.run_id
    assert cap.provenance.transcript_sha256 and "discovery-v1" in cap.provenance.recorder_version
    assert "member_not_found" in cap.use_profile_outcomes
    s01 = cap.steps[0].expect
    assert {c.kind for c in s01.all_of} == {"url_matches", "text_present"}
    assert cap.steps[3].expect.all_of[1].text == "MEMBER DETAIL - {{inputs.member_id}}"

    run = d.run_dir
    assert {p.name for p in run.iterdir()} >= {"events.jsonl", "transcript.jsonl",
                                               "manifest.json", "summary.json", "screenshots"}
    assert (run / "screenshots" / "step01.png").exists()
    summary = json.loads((run / "summary.json").read_text())
    assert summary["status"] == "completed" and summary["outputs"] == {
        "savings_balance": "12345.67"}
    types = {e["type"] for e in events(run)}
    assert {"run_started", "llm_decision", "policy_decision", "action", "checkpoint_generated",
            "run_finished"} <= types

    # A second discovery of the same capability becomes the next MINOR version.
    again = discover(app_a, read_balance_script())
    assert again.result.artifact_path.endswith("/1.1.0.json")


def test_literal_member_number_is_parameterized_automatically(app_a, discover):
    d = discover(app_a, read_balance_script(member_value="12345"))
    assert d.result.status == "completed" and d.cap is not None, d.result.lint_errors
    assert d.cap.steps[1].action.value == "{{inputs.member_id}}"
    warnings = [e["data"] for e in events(d.run_dir) if e["type"] == "warning"]
    assert {"code": "parameterized_literal", "input": "member_id", "tool": "fill"} in warnings


def test_invalid_ref_gets_an_error_result_then_recovers(app_a, discover):
    script = read_balance_script()
    script.insert(0, click("e999"))
    d = discover(app_a, script)
    assert d.result.status == "completed"
    second_prompt = d.llm.calls[1][1][-1]
    [result] = [b for b in second_prompt.content if b.type == "tool_result"]
    assert result.is_error and "e999" in result.content
    assert "LAST ACTION RESULT: error: invalid tool call" in second_prompt.text()


def test_three_invalid_calls_escalate(app_a, discover):
    bad = [click("e999"), fill("@textbox:NOPE", "x"),
           T(name="press", args={"key": "F5", "intent": "reload the page"})]
    d = discover(app_a, bad)
    assert d.result.status == "escalated"
    assert "3 consecutive invalid tool calls" in d.result.reason
    assert d.result.artifact_path is None
    assert any(e["type"] == "escalation" for e in events(d.run_dir))


def test_no_progress_escalates(app_a, discover):
    d = discover(app_a, [click("@text:FUNCTIONS")] * 5)
    assert d.result.status == "escalated" and "no progress" in d.result.reason
    assert d.result.turns == 3


def test_max_steps_escalates(app_a, discover):
    flip = [click("@button:MEMBER INQUIRY"), click("@link:OPEN SUB-ACCOUNT")] * 5
    d = discover(app_a, flip, max_steps=4)
    assert d.result.status == "escalated" and "max_steps" in d.result.reason
    assert len(d.llm.calls) == 4


def test_unknown_member_is_a_business_outcome(app_a, discover):
    script = read_balance_script()[:3] + [done("NO RECORDS FOUND for the member.")]
    d = discover(app_a, script, member="99999")
    assert d.result.status == "business_outcome"
    assert d.result.outcome == "MEMBER_NOT_FOUND"
    assert d.result.artifact_path is None
    [ev] = [e for e in events(d.run_dir) if e["type"] == "outcome"]
    assert ev["data"]["outcome"] == "member_not_found"


def test_done_without_outputs_is_rejected_and_counts_as_invalid(app_a, discover):
    d = discover(app_a, [done(), done(), done()])
    assert d.result.status == "escalated"
    assert "outputs not extracted" in d.llm.calls[1][1][-1].text()


OPEN_INPUTS = [
    {"name": "member_id", "value": "22222", "type": "string", "pattern": r"^\d{5}$"},
    {"name": "account_type", "value": "Money Market", "type": "enum",
     "enum_values": ["Share Savings", "Money Market", "Certificate 12mo"]},
    {"name": "initial_deposit", "value": "250.00", "type": "decimal", "min": "0.01",
     "max": "10000"},
]
OPEN_OUTPUTS = [{"name": "confirmation_number", "type": "string", "normalize": "strip",
                 "extract_pattern": r"(MC-\d{6})"}]


def open_script() -> list:
    return [
        click("@link:OPEN SUB-ACCOUNT", "Open the sub-account form."),
        fill("@textbox:MEMBER NO.", "{{inputs.member_id}}"),
        select("@combobox:ACCOUNT TYPE", "Money Market"),
        fill("@textbox:INITIAL DEPOSIT", "{{inputs.initial_deposit}}"),
        click("@button:CONTINUE", "Go to the review page."),
        click("@button:SUBMIT", "Commit the sub-account."),
        _confirmation_extract,
        done("Opened the sub-account."),
    ]


def open_inputs(member: str) -> list[dict]:
    return [{**OPEN_INPUTS[0], "value": member}, *OPEN_INPUTS[1:]]


def accounts_of(base_url: str, member: str) -> str:
    """The member's ACCOUNTS table, read over plain HTTP with its own operator session."""
    with httpx.Client(base_url=base_url) as http:
        http.post("/login", data={"f1": "demo_operator", "f2": "demo-pass-123"})
        page = http.get(f"/member/detail?m={member}").text
    return page[page.index("ACCOUNTS"):]


def _confirmation_extract(messages):
    text = messages[-1].text()
    ref = next(line.split("]")[0][1:] for line in text.splitlines()
               if "CONFIRMATION NO." in line and line.startswith("[e"))
    return T(name="extract", args={"ref": ref, "output": "confirmation_number",
                                   "intent": "Read the confirmation number."})


def test_irreversible_submit_denied_escalates_and_creates_nothing(app_a, discover):
    before = accounts_of(app_a, "70001")
    d = discover(app_a, open_script(), inputs=open_inputs("70001"), outputs=OPEN_OUTPUTS,
                 capability_id="meridian.subaccount.open", approver=DenyAllApprover(),
                 goal="Open a Money Market sub-account for member 70001 with 250.00")
    assert d.result.status == "escalated" and "ApprovalDenied" in d.result.reason
    assert d.result.artifact_path is None
    approvals = [e for e in events(d.run_dir) if e["type"] == "approval_result"]
    assert [a["data"]["approved"] for a in approvals] == [False]
    assert accounts_of(app_a, "70001") == before  # no sub-account was created
    assert "Money Market" not in before


def test_irreversible_submit_with_approval_is_recorded_as_gated(app_a, discover):
    approver = AutoApproveForTests()
    d = discover(app_a, open_script(), inputs=open_inputs("22222"), outputs=OPEN_OUTPUTS,
                 capability_id="meridian.subaccount.open", approver=approver,
                 goal="Open a Money Market sub-account for member 22222 with 250.00")
    assert d.result.status == "completed", d.result.reason
    assert d.result.outputs["confirmation_number"].startswith("MC-")
    [submit] = [s for s in d.cap.steps if s.id.endswith("_click_submit")]
    assert (submit.risk, submit.requires_approval, submit.approved) == (
        "irreversible", True, False)
    assert d.cap.steps[2].action.option == "{{inputs.account_type}}"  # literal parameterized
    extract_step = d.cap.steps[-1]
    assert "MC-" not in extract_step.action.target.model_dump_json().replace(
        "CONFIRMATION NO. MC-", "")  # the run's confirmation number is not baked in
    assert [d_.rule_id for d_, _ in approver.requests] == ["subacct-submit"]
    assert "Money Market" in accounts_of(app_a, "22222")
    assert d.result.irreversible_executed
    assert d.result.irreversible_steps_executed[0].endswith('click button "SUBMIT"')


def test_interstitial_mid_run_is_recovered_without_asking_the_model(app_a, discover):
    script = read_balance_script()

    def nav_with_fault(messages):
        set_faults(app_a, interstitial_count=1)  # the next main-frame page is the notice
        return click("@button:MEMBER INQUIRY", "Open the member inquiry screen.")

    script[0] = nav_with_fault
    d = discover(app_a, script)
    assert d.result.status == "completed", d.result.reason
    assert len(d.llm.calls) == len(script)  # no extra LLM turn for the interstitial
    second = d.llm.calls[1][1][-1].text()
    assert "SYSTEM NOTICE" not in second and 'textbox "MEMBER NO."' in second
    recoveries = [e["data"] for e in events(d.run_dir) if e["type"] == "recovery"]
    assert recoveries == [{"kind": "interstitial", "id": "maintenance_notice", "count": 1}]
    assert all("[CONTINUE]" not in s.model_dump_json() for s in d.cap.steps)


def test_build_failure_after_irreversible_submit_keeps_evidence(app_a, discover, monkeypatch):
    """Regression for run 2: a crash after the loop must not lose what was executed."""
    from cua.discovery.recorder import Recorder

    def broken_build(self, *a, **k):
        raise RuntimeError("enum_values is required for type=enum")

    monkeypatch.setattr(Recorder, "build", broken_build)
    d = discover(app_a, open_script(), inputs=open_inputs("55555"), outputs=OPEN_OUTPUTS,
                 capability_id="meridian.subaccount.open", approver=AutoApproveForTests(),
                 goal="Open a Money Market sub-account for member 55555 with 250.00")
    r = d.result
    assert r.status == "error" and r.artifact_path is None
    assert "RuntimeError during artifact build" in r.reason
    assert r.irreversible_executed is True
    assert "Money Market" in accounts_of(app_a, "55555")  # it really happened

    summary = json.loads((d.run_dir / "summary.json").read_text())
    assert summary["status"] == "error"
    assert "RuntimeError" in summary["reason"] and "enum_values" in summary["reason"]
    assert summary["irreversible_executed"] is True
    [submit] = summary["irreversible_steps_executed"]
    assert 'click button "SUBMIT"' in submit
    actions = summary["executed_actions"]
    [gated] = [a for a in actions if a["risk"] == "irreversible"]
    assert gated["rule_id"] == "subacct-submit" and gated["ok"] is True
    errors = [e["data"] for e in events(d.run_dir) if e["type"] == "error"]
    [err] = [e for e in errors if e.get("phase") == "artifact build"]
    assert err["irreversible_executed"] is True and err["error_type"] == "RuntimeError"
    assert any(e.get("snapshot") for e in errors)  # failure snapshot + screenshot logged
    assert list((d.run_dir / "snapshots").glob("*failure.txt"))
    assert json.loads((d.run_dir / "manifest.json").read_text())["status"] == "error"


def test_irreversible_flags_are_reported_generally(app_a, discover):
    d = discover(app_a, read_balance_script())
    assert d.result.irreversible_executed is False
    assert d.result.irreversible_steps_executed == []
    summary = json.loads((d.run_dir / "summary.json").read_text())
    assert summary["irreversible_executed"] is False
    assert [a["action"] for a in summary["executed_actions"]][:2] == [
        'click button "MEMBER INQUIRY"', 'fill textbox "MEMBER NO."']
