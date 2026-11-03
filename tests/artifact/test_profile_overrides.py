"""App profile YAML and tenant overrides (pure tests)."""

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from cua.artifact import store
from cua.artifact.overrides import OverrideError, Patch, TenantOverride, apply_override
from cua.artifact.profile import PROFILES_DIR, AppProfile
from cua.cli import app
from cua.policy.models import PolicyConfig
from tests.artifact.conftest import OPEN_SUBACCOUNT, READ_BALANCE

TENANT_B = PROFILES_DIR / "tenants" / "tenant-b.yaml"


@pytest.fixture(scope="module")
def profile() -> AppProfile:
    return AppProfile.load_named("meridian-core")


def test_profile_yaml_validates_and_lints_clean(profile):
    report = profile.lint()
    assert report.ok and report.warnings == [], report.format()
    assert [s.id for s in profile.login_subflow] == [
        "l01_open_sign_on", "l02_fill_operator_id", "l03_fill_password", "l04_click_sign_on"]
    assert profile.login_subflow[2].action.value == "{{secret:BANK_OPERATOR_PASS}}"
    assert {o.code for o in profile.outcomes} == {
        "MEMBER_NOT_FOUND", "INVALID_MEMBER_FORMAT", "ACCESS_DENIED", "DEPOSIT_OUT_OF_RANGE",
        "INVALID_DEPOSIT_AMOUNT"}
    assert profile.dialog_response("Unposted items exist. Continue?") == "accept"
    assert profile.dialog_response("something else") is None
    result = CliRunner().invoke(app, ["profile", "validate",
                                      str(PROFILES_DIR / "meridian-core.yaml")])
    assert result.exit_code == 0 and "OK meridian-core" in result.output


def test_sensitive_labels_single_source_of_truth(profile):
    policy = PolicyConfig.load()
    assert policy.sensitive_labels_from == "app_profiles/meridian-core.yaml"
    assert policy.sensitive_labels == profile.sensitive_labels
    assert "NAME" in profile.sensitive_labels


def test_profile_lint_rejects_inputs_literal_passwords_and_bad_outcome_scope(profile):
    data = profile.model_dump(mode="json", exclude_none=True)
    data["login_subflow"][1]["action"]["value"] = "{{inputs.user}}"
    data["login_subflow"][2]["action"]["value"] = "hunter22"
    data["outcomes"][0]["check_after_steps"] = ["s03_click_inquire"]
    data["sensitive_labels"] = []
    with pytest.raises(ValidationError) as exc:
        AppProfile.model_validate(data)
    msg = str(exc.value)
    for code in ("input_not_allowed", "sensitive_literal", "unknown_step_reference",
                 "no_sensitive_labels"):
        assert f"[{code}]" in msg


def test_tenant_b_override_applies_and_lints_clean(profile):
    override = TenantOverride.load(TENANT_B)
    cap, _ = store.load(READ_BALANCE)
    patched = apply_override(cap, override)
    assert patched.lint().ok
    fill = patched.steps[1].action.target
    assert [(s.kind, getattr(s, "label", None)) for s in fill.strategies][:2] == [
        ("label_anchor", "ACCT HOLDER #"), ("label_anchor", "MEMBER NO.")]
    inquire = patched.steps[2].action.target
    assert inquire.strategies[0].name == "SEARCH" and inquire.description == "SEARCH button"
    assert cap.steps[2].action.target.strategies[0].name == "INQUIRE"  # pure: input untouched
    assert patched.content_hash() != cap.content_hash()
    assert patched.inputs == cap.inputs and patched.outputs == cap.outputs  # contract intact

    opened = apply_override(store.load(OPEN_SUBACCOUNT)[0], override)
    assert opened.steps[1].action.target.strategies[0].label == "ACCT HOLDER #"
    assert opened.steps[6].risk == "irreversible"

    p = apply_override(profile, override)
    assert p.outcome("member_not_found").message_template.endswith("account holder number.")
    assert p.lint().ok


def test_override_scope_and_errors(profile):
    cap, _ = store.load(READ_BALANCE)
    v2 = cap.model_copy(update={"version": "2.0.0"})
    override = TenantOverride.load(TENANT_B)
    with pytest.raises(OverrideError, match="does not apply"):
        apply_override(v2, override)  # MAJOR pin "1.x"

    def one(**patch) -> TenantOverride:
        return TenantOverride(tenant="t", reason="r", applies_to=[
            {"capability": cap.id, "versions": "1.x"}],
            patches=[{"capability": cap.id, **patch}])

    with pytest.raises(OverrideError, match="not found"):
        apply_override(cap, one(at="s99_nope", op="set_field", path="intent", value="x"))
    with pytest.raises(OverrideError, match="not found"):
        apply_override(cap, one(at="s02_fill_member_no", op="replace_text",
                                path="action.target.description", old="ZZZ", value="y"))
    with pytest.raises(OverrideError, match="TargetSpec"):
        apply_override(cap, one(at="s02_fill_member_no", op="prepend_strategy", path="intent",
                                value={"kind": "text_match", "text": "x"}))
    with pytest.raises(ValidationError, match="risk/approval"):
        Patch(at="s07_submit", capability=cap.id, op="set_field", path="risk", value="safe")
    with pytest.raises(ValidationError, match="must name its capability"):
        Patch(at="s01", op="set_field", path="intent", value="x")
    with pytest.raises(ValidationError, match="template_in_target"):  # re-linted after patching
        apply_override(cap, one(at="s03_click_inquire", op="replace_strategy",
                                path="action.target",
                                value={"kind": "role_name", "role": "link",
                                       "name": "{{inputs.member_id}}"}))
