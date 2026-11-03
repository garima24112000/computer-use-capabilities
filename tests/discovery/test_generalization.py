"""Step 5b: discovered artifacts must generalize beyond the run that recorded them."""

import json

from cua.artifact import store
from cua.surface.locators import CssPath, TableCell, TextMatch
from tests.discovery.conftest import T, click, done, fill, read_balance_script, select

DEPOSIT_OUTCOMES = {"deposit_out_of_range", "invalid_deposit_amount"}


def _intent_literal_script():
    s = read_balance_script()
    s[1] = fill("@textbox:MEMBER NO.", "{{inputs.member_id}}", "Type 12345 into the field.")
    return s


def test_no_input_literal_in_free_text_and_description_is_not_the_goal(app_a, discover):
    d = discover(app_a, _intent_literal_script())
    cap = d.cap
    assert cap.description == cap.title == "Read a member's share savings balance"
    assert "Look up member" not in cap.description  # the goal stays in provenance only
    assert cap.provenance.goal_redacted.startswith("Look up member 12345")
    assert cap.steps[1].intent == "Type {{inputs.member_id}} into the field."
    free = [cap.title, cap.description, cap.success.description]
    for s in cap.steps:
        free += [s.intent, s.notes or "", s.action.target.description]
        if s.expect:
            free.append(s.expect.description)
    assert not any("12345" in t for t in free)
    assert "input_literal_in_text" not in cap.lint().codes()


def test_explicit_description_is_used(app_a, discover):
    d = discover(app_a, read_balance_script(), description="Balance of member {{x}}"[:16])
    assert d.cap.description == "Balance of membe"


def test_inquire_gets_a_postcondition_and_outcomes_are_scoped(app_a, discover):
    cap = discover(app_a, read_balance_script()).cap
    inquire = cap.steps[2]
    assert inquire.id == "s03_click_inquire" and inquire.expect is not None
    names = [c.target.strategies[0].name for c in inquire.expect.all_of]
    assert names == ["NAME", "BRANCH"]  # newly appeared grid headers; MEMBER NO. existed
    assert set(cap.use_profile_outcomes) == {"member_not_found", "invalid_member_format",
                                             "access_denied"}
    assert not DEPOSIT_OUTCOMES & set(cap.use_profile_outcomes)


def test_savings_extract_prefers_the_business_label_row_key(app_a, discover):
    cap = discover(app_a, read_balance_script()).cap
    strategies = cap.steps[-1].action.target.strategies
    assert strategies[0] == TableCell(section_header="ACCOUNTS", row_key="Share Savings",
                                      column_header="BALANCE")
    assert strategies[1].row_key == "00" and isinstance(strategies[-1], CssPath)
    shown = cap.summary_markdown()
    assert "table_cell(row='Share Savings', col='BALANCE') → table_cell(row='00'" in shown


OPEN_INPUTS = [
    {"name": "member_id", "value": "31415", "type": "string", "pattern": r"^\d{5}$"},
    {"name": "account_type", "value": "Money Market", "type": "enum",
     "enum_values": ["Share Savings", "Money Market", "Certificate 12mo"]},
    {"name": "initial_deposit", "value": "250.00", "type": "decimal", "min": "0.01",
     "max": "10000", "description": "Initial deposit in dollars"},
]


def _open_script():
    def extract_banner(messages):
        text = messages[-1].text()
        ref = next(line.split("]")[0][1:] for line in text.splitlines()
                   if "CONFIRMATION NO." in line and line.startswith("[e"))
        return T(name="extract", args={"ref": ref, "output": "confirmation_number",
                                       "intent": "Read the confirmation number."})

    return [click("@link:OPEN SUB-ACCOUNT"), fill("@textbox:MEMBER NO.", "31415"),
            select("@combobox:ACCOUNT TYPE", "Money Market"),
            fill("@textbox:INITIAL DEPOSIT", "250.00"), click("@button:CONTINUE"),
            click("@button:SUBMIT"), extract_banner, done("Opened.")]


def _discover_open(app_a, discover, outputs):
    return discover(app_a, _open_script(), inputs=OPEN_INPUTS, outputs=outputs,
                    capability_id="meridian.subaccount.open",
                    goal="Open a Money Market sub-account for member 31415 with 250.00")


def test_subaccount_contract_bounds_and_stable_confirmation_extract(app_a, discover):
    d = _discover_open(app_a, discover, [
        {"name": "confirmation_number", "type": "string", "normalize": "strip",
         "extract_pattern": r"(MC-\d{6})"}])
    r, cap = d.result, d.cap
    assert r.status == "completed" and cap is not None, r.lint_errors
    assert r.outputs["confirmation_number"].startswith("MC-")
    assert len(r.outputs["confirmation_number"]) == 9  # "MC-xxxxxx" only, not the banner
    deposit = next(p for p in cap.inputs if p.name == "initial_deposit")
    assert (str(deposit.min), str(deposit.max)) == ("0.01", "10000")
    assert deposit.description == "Initial deposit in dollars"
    assert cap.outputs[0].extract_pattern == r"(MC-\d{6})"
    target = cap.steps[-1].action.target
    assert isinstance(target.strategies[0], TextMatch)
    assert target.strategies[0].text == "SUB-ACCOUNT OPENED — CONFIRMATION NO."
    assert not target.strategies[0].exact
    assert r.outputs["confirmation_number"] not in target.model_dump_json()
    assert DEPOSIT_OUTCOMES <= set(cap.use_profile_outcomes)  # /subacct/* pages visited


def test_structural_only_extract_is_rejected_with_a_clear_reason(app_a, discover):
    d = _discover_open(app_a, discover, [  # no extract_pattern: no stable text remains
        {"name": "confirmation_number", "type": "string", "normalize": "strip"}])
    r = d.result
    assert r.status == "completed" and r.artifact_path is None
    assert r.rejected_path.endswith("1.0.0.rejected.json")
    assert any("extract_structural_only" in e for e in r.lint_errors)
    saved = json.loads(open(r.rejected_path).read())
    assert any("extract_structural_only" in e for e in saved["lint_errors"])


def test_a_second_discovery_becomes_1_1_0(app_a, discover):
    discover(app_a, read_balance_script())
    again = discover(app_a, read_balance_script())
    assert again.result.artifact_path.endswith("/1.1.0.json")
    assert store.load(again.result.artifact_path)[0].version == "1.1.0"
