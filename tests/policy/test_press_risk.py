"""Pressing Enter is classified by the form submit control it would trigger, not as `safe`."""

import json

import pytest

from cua.policy.errors import ApprovalDenied
from cua.surface.locators import LabelAnchor, RoleName, TargetSpec
from cua.surface.models import FrameRef
from tests.surface.helpers import MAIN, SUBMIT, TOP, goto_inquiry, login, open_review

SECRET_TEMPLATE = "{{secret:BANK_OPERATOR_PASS}}"
MEMBER_NO = TargetSpec(frame=MAIN, description="member no",
                       strategies=[LabelAnchor(label="MEMBER NO.", role="textbox")])
COMMIT_FORM = """<html><body><form name="frmT" action="/nowhere">
<table><tr><td>AMOUNT</td><td><input type="text" name="amt"></td></tr>
<tr><td></td><td><input type="submit" value="Transfer funds"></td></tr></table></form>
<p>plain text</p></body></html>"""


@pytest.fixture(autouse=True)
def _operator_secret(monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")


def decisions(run) -> list[dict]:
    lines = run.events_path.read_text().splitlines()
    return [e["data"]["decision"] for e in map(json.loads, lines)
            if e["type"] == "policy_decision" and e["data"]["action"].startswith("press")]


def test_enter_in_login_password_is_classified_by_sign_on(app_a, make_guarded):
    g = make_guarded()
    g.surface.open(f"{app_a}/login")
    g.surface.fill(TargetSpec(frame=TOP, description="op", strategies=[
        LabelAnchor(label="OPERATOR ID", role="textbox")]), "demo_operator")
    password = TargetSpec(frame=TOP, description="pw",
                          strategies=[LabelAnchor(label="PASSWORD", role="textbox")])
    g.surface.fill(password, SECRET_TEMPLATE)
    result = g.surface.press("Enter", password)
    # The SIGN ON div calls document.frmA.submit(): a named-form JS submit control.
    assert result.policy.rule_id == "press-submit:default:click"
    assert result.policy.risk == "risky" and result.policy.verdict == "allow"
    assert "SIGN ON" in result.policy.reason
    assert g.surface.page_contains("FUNCTIONS", FrameRef(name_path=["nav"]))


def test_enter_in_inquiry_field_is_risky_and_submits(app_a, make_guarded):
    g = make_guarded()
    login(g.surface, app_a, password=SECRET_TEMPLATE)
    goto_inquiry(g.surface)
    g.surface.fill(MEMBER_NO, "12345")
    result = g.surface.press("Enter", MEMBER_NO)
    assert result.policy.risk == "risky"
    assert result.policy.rule_id == "press-submit:default:click"  # INQUIRE link
    assert g.surface.page_contains("BRANCH", MAIN)


def test_enter_on_focused_submit_image_is_irreversible_and_denied(app_a, make_guarded):
    g = make_guarded()
    login(g.surface, app_a, password=SECRET_TEMPLATE)
    open_review(g.surface)
    with pytest.raises(ApprovalDenied) as exc:
        g.surface.press("Enter", SUBMIT)
    assert exc.value.decision.rule_id == "press-submit:subacct-submit"
    assert exc.value.decision.risk == "irreversible"
    assert g.surface.page_contains("OPEN SUB-ACCOUNT - REVIEW", MAIN)
    assert not g.surface.page_contains("SUB-ACCOUNT OPENED", MAIN)


def test_enter_in_focused_field_of_commit_form_is_irreversible(make_guarded):
    g = make_guarded()
    page = g.surface.inner.page
    page.set_content(COMMIT_FORM)
    page.locator("input[name=amt]").focus()
    with pytest.raises(ApprovalDenied) as exc:
        g.surface.press("Enter")  # no target: the focused element is found via the scan
    assert exc.value.decision.rule_id == "press-submit:generic-commit-verb"
    assert decisions(g.run)[-1]["verdict"] == "require_approval"


def test_enter_with_unknown_submit_control_is_risky(make_guarded):
    g = make_guarded()
    g.surface.inner.page.set_content(COMMIT_FORM)  # nothing focused
    result = g.surface.press("Enter")
    assert result.policy.rule_id == "press-submit:unknown"
    assert result.policy.risk == "risky"


def test_space_in_text_field_and_other_keys_keep_default_risk(make_guarded):
    g = make_guarded()
    page = g.surface.inner.page
    page.set_content(COMMIT_FORM)
    page.locator("input[name=amt]").focus()
    assert g.surface.press("Space").policy.rule_id == "default:press"
    assert g.surface.press("Tab").policy.risk == "safe"
    page.locator("input[type=submit]").focus()
    assert g.surface.key_context().focused.role == "button"
    with pytest.raises(ApprovalDenied):  # Space on the focused commit button == clicking it
        g.surface.press("Space")


def test_key_context_reports_form_submit_controls(app_a, make_surface):
    s = make_surface()
    login(s, app_a)
    open_review(s)
    kc = s.key_context(SUBMIT)
    assert kc.known and [c.name for c in kc.submit_controls] == ["SUBMIT"]
    goto_inquiry(s)
    kc = s.key_context(MEMBER_NO)
    assert [(c.role, c.name) for c in kc.submit_controls] == [("link", "INQUIRE")]
    assert s.key_context(TargetSpec(frame=MAIN, description="x", strategies=[
        RoleName(role="link", name="INQUIRE")])).submit_controls[0].name == "INQUIRE"
