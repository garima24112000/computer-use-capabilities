"""Every lint rule has a failing case. Lint reports all problems at once."""

from collections.abc import Callable

import pytest

from tests.artifact.conftest import OPEN_SUBACCOUNT, lint_errors, raw, validate

MAIN = {"name_path": ["main"]}


def step(data: dict, step_id: str) -> dict:
    return next(s for s in data["steps"] if s["id"] == step_id)


def target(*strategies: dict) -> dict:
    return {"frame": MAIN, "description": "t", "strategies": list(strategies)}


def _dup_step(d):
    d["steps"][2]["id"] = d["steps"][0]["id"]


def _undeclared_input(d):
    step(d, "s02_fill_member_no")["action"]["value"] = "{{inputs.member_no}}"


def _unused_required(d):
    d["inputs"].append({"name": "branch", "type": "string", "description": "unused"})


def _output_not_extracted(d):
    d["outputs"].append({"name": "checking_balance", "type": "money", "description": "x"})


def _output_twice(d):
    d["steps"].append({**step(d, "s05_extract_savings_balance"), "id": "s06_again"})


def _extract_undeclared(d):
    step(d, "s05_extract_savings_balance")["action"]["output"] = "nope"


def _irreversible_ungated(d):
    step(d, "s04_open_member_detail")["risk"] = "irreversible"


def _sensitive_literal_pattern(d):
    step(d, "s02_fill_member_no")["action"]["value"] = "900-12-3456"


def _sensitive_literal_label(d):
    step(d, "s02_fill_member_no")["action"]["target"] = target(
        {"kind": "label_anchor", "label": "SSN", "role": "textbox"})
    step(d, "s02_fill_member_no")["action"]["value"] = "plain words"


def _sensitive_field_input(d):
    step(d, "s02_fill_member_no")["action"]["target"] = target(
        {"kind": "label_anchor", "label": "PASSWORD", "role": "textbox"})


def _sensitive_in_target(d):
    step(d, "s04_open_member_detail")["action"]["target"] = target(
        {"kind": "role_name", "role": "link", "name": "john.sample@example.com"})


def _template_in_target(d):
    step(d, "s04_open_member_detail")["action"]["target"] = target(
        {"kind": "role_name", "role": "link", "name": "{{inputs.member_id}}"})


def _parameterized_sensitive_input(d):
    d["inputs"][0]["sensitive"] = True
    d["inputs"][0].pop("example")


def _unknown_step_ref(d):
    d["outcomes"].append({
        "id": "no_savings", "code": "NO_SAVINGS", "description": "x", "message_template": "x",
        "detect": {"description": "x", "all_of": [{"kind": "text_absent", "text": "Share"}]},
        "check_after_steps": ["s99_missing"]})


def _undeclared_secret(d):
    step(d, "s02_fill_member_no")["action"]["value"] = "{{secret:MEMBER_PIN}}"


def _sensitive_checkpoint_text(d):
    d["success"]["all_of"].append({"kind": "text_present", "text": "SSN 900-12-3456"})


def _approved_without_approval(d):
    d["status"] = "approved"


def _duplicate_input(d):
    d["inputs"].append(dict(d["inputs"][0]))


def _duplicate_outcome(d):
    d["use_profile_outcomes"].append("member_not_found")


def _sensitive_goal(d):
    d["provenance"]["goal_redacted"] = "read balance for SSN 900-12-3456"


RULES: list[tuple[str, Callable[[dict], None]]] = [
    ("duplicate_step_id", _dup_step),
    ("undeclared_input", _undeclared_input),
    ("unused_required_input", _unused_required),
    ("output_not_extracted", _output_not_extracted),
    ("output_extracted_multiple", _output_twice),
    ("extract_undeclared_output", _extract_undeclared),
    ("irreversible_without_approval", _irreversible_ungated),
    ("sensitive_literal", _sensitive_literal_pattern),
    ("sensitive_literal", _sensitive_literal_label),
    ("sensitive_field_input_not_sensitive", _sensitive_field_input),
    ("sensitive_in_target", _sensitive_in_target),
    ("template_in_target", _template_in_target),
    ("parameterized_target_input", _parameterized_sensitive_input),
    ("unknown_step_reference", _unknown_step_ref),
    ("undeclared_secret", _undeclared_secret),
    ("sensitive_in_checkpoint", _sensitive_checkpoint_text),
    ("review_missing", _approved_without_approval),
    ("duplicate_input", _duplicate_input),
    ("duplicate_outcome", _duplicate_outcome),
    ("sensitive_in_provenance", _sensitive_goal),
]


@pytest.mark.parametrize("code, mutate", RULES, ids=[f"{c}-{m.__name__}" for c, m in RULES])
def test_each_lint_rule_has_a_failing_case(read_raw, code, mutate):
    mutate(read_raw)
    assert f"[{code}]" in lint_errors(read_raw)


def test_fixtures_lint_clean():
    for path in (None, OPEN_SUBACCOUNT):
        cap = validate(raw() if path is None else raw(path))
        report = cap.lint()
        assert report.ok and report.warnings == [], report.format()


def test_all_problems_are_reported_not_just_the_first(read_raw):
    _dup_step(read_raw)
    _undeclared_input(read_raw)
    _extract_undeclared(read_raw)
    msg = lint_errors(read_raw)
    for code in ("duplicate_step_id", "undeclared_input", "extract_undeclared_output",
                 "output_not_extracted"):
        assert f"[{code}]" in msg
    assert "lint error(s)" in msg


def test_approved_status_requires_every_gated_step_approved(open_raw):
    open_raw["status"] = "approved"
    open_raw["review"] = {"reviewed_by": "x", "approved_at": "2026-11-03T00:00:00Z",
                          "approved_content_hash": "sha256:0"}
    msg = lint_errors(open_raw)
    assert "[approval_missing]" in msg and "s07_submit" in msg


def test_warnings_do_not_block(read_raw):
    s = step(read_raw, "s04_open_member_detail")
    s["action"]["target"] = target({"kind": "css_path", "selector": "html > body"})
    s.pop("expect")
    report = validate(read_raw).lint()
    assert report.ok and {"brittle_target", "unverified_step"} <= report.codes()


def test_extra_fields_and_off_grammar_templates_are_rejected(read_raw):
    read_raw["steps"][0]["actoin"] = {}
    assert "actoin" in lint_errors(read_raw)
    data = raw()
    step(data, "s02_fill_member_no")["action"]["value"] = "{{ inputs.member_id }}"
    assert "unsupported placeholder" in lint_errors(data)
    data = raw()  # secrets only as a whole Fill value: other texts are input-only templates
    data["steps"][0]["expect"]["all_of"].append({"kind": "text_present", "text": "{{secret:X}}"})
    assert "secret placeholders are not allowed here" in lint_errors(data)
    data = raw()
    data["steps"][0]["action"] = {"type": "navigate", "url_path": "https://evil.example/"}
    assert "app-relative path" in lint_errors(data)


def test_extract_structural_only_is_error_for_discovery_warning_for_hand_written(read_raw):
    extract = step(read_raw, "s05_extract_savings_balance")
    extract["action"]["target"]["strategies"] = [{"kind": "css_path", "selector": "html > b"}]
    report = validate(read_raw).lint()
    assert report.ok and "extract_structural_only" in report.codes()  # hand_written: warning
    read_raw["provenance"]["source"] = "discovery"
    assert "[extract_structural_only]" in lint_errors(read_raw)


def test_input_literal_in_free_text_is_a_warning(read_raw):
    read_raw["description"] = "Reads the balance for member 12345."
    step(read_raw, "s03_click_inquire")["intent"] = "Search for 12345."
    warnings = [w for w in validate(read_raw).lint().warnings
                if w.code == "input_literal_in_text"]
    assert [w.path for w in warnings] == ["description", "steps[2:s03_click_inquire].intent"]
