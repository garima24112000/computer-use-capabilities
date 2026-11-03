"""The hand-written fixtures and the profile, checked against the real app in headless Chromium.

`drive()` is a deliberately naive step runner for this check only: it performs actions and
checks text expectations. It is NOT the replay engine (step 6), which adds outcomes,
interstitials, retries and failure reporting.
"""

import pytest

from cua.artifact import store
from cua.artifact.conditions import TargetPresent, TextPresent
from cua.artifact.overrides import TenantOverride, apply_override
from cua.artifact.profile import PROFILES_DIR, AppProfile
from cua.artifact.steps import Click, Extract, Fill, Navigate, Press, Select, Step
from cua.templating import render
from tests.artifact.conftest import OPEN_SUBACCOUNT, READ_BALANCE
from tests.surface.helpers import MAIN, set_faults


@pytest.fixture(autouse=True)
def _operator_secrets(monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_USER", "demo_operator")
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")


@pytest.fixture(scope="module")
def profile() -> AppProfile:
    return AppProfile.load_named("meridian-core")


def drive(s, base: str, steps: list[Step], inputs: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for step in steps:
        a = step.action
        t = a.target.bind(inputs) if getattr(a, "target", None) is not None else None
        if isinstance(a, Navigate):
            s.open(base + render(a.url_path, inputs))
        elif isinstance(a, Click):
            s.click(t)
        elif isinstance(a, Fill):
            s.fill(t, render(a.value, inputs))
        elif isinstance(a, Select):
            s.select(t, render(a.option, inputs))
        elif isinstance(a, Press):
            s.press(a.key, t)
        elif isinstance(a, Extract):
            out[a.output] = s.read_text(t)
        for _, c in (step.expect.conditions() if step.expect else []):
            if isinstance(c, TextPresent):
                assert s.page_contains(render(c.text, inputs), c.frame), (step.id, c)
            elif isinstance(c, TargetPresent):
                s.resolve(c.target.bind(inputs))
    return out


def test_read_savings_balance_fixture_runs_on_tenant_a(app_a, make_guarded, profile):
    g = make_guarded()
    cap, _ = store.load(READ_BALANCE)
    drive(g.surface, app_a, profile.login_subflow, {})
    raw = drive(g.surface, app_a, cap.steps, {"member_id": "12345"})
    assert cap.outputs[0].normalize_value(raw["savings_balance"]) == "12345.67"
    assert "demo-pass-123" not in g.run.events_path.read_text()


def test_tenant_b_override_makes_the_fixture_run_on_tenant_b(app_b, make_guarded, profile):
    override = TenantOverride.load(PROFILES_DIR / "tenants" / "tenant-b.yaml")
    cap = apply_override(store.load(READ_BALANCE)[0], override)
    g = make_guarded()
    drive(g.surface, app_b, apply_override(profile, override).login_subflow, {})
    raw = drive(g.surface, app_b, cap.steps, {"member_id": "31415"})
    assert cap.outputs[0].normalize_value(raw["savings_balance"]) == "3141.59"


def test_open_subaccount_fixture_with_approved_submit(app_a, make_guarded, profile):
    g = make_guarded(mode="replay", artifact_approved=True, step_approved=True)
    cap, _ = store.load(OPEN_SUBACCOUNT)
    drive(g.surface, app_a, profile.login_subflow, {})
    inputs = {"member_id": "55555", "account_type": "Money Market", "initial_deposit": "250.00",
              "nickname": "Rainy day"}
    assert cap.validate_inputs(inputs) == []
    raw = drive(g.surface, app_a, cap.steps, inputs)
    conf = cap.outputs[0].normalize_value(raw["confirmation_number"])
    assert conf.startswith("MC-") and len(conf) == 9


def test_profile_detectors_and_recovery_match_the_real_app(app_a, make_guarded, profile):
    g = make_guarded()
    s = g.surface
    drive(s, app_a, profile.login_subflow, {})
    cap, _ = store.load(READ_BALANCE)

    set_faults(app_a, interstitial_count=1)
    s.click(cap.steps[0].action.target)  # the interstitial replaces /inquiry
    [inter] = profile.interstitials
    [(_, detect)] = list(inter.detect.conditions())
    assert s.page_contains(detect.text, detect.frame)
    s.click(inter.recover[0].target)
    assert s.page_contains("MEMBER INQUIRY", MAIN)

    s.fill(cap.steps[1].action.target, "99999")
    s.click(cap.steps[2].action.target)
    not_found = profile.outcome("member_not_found")
    [(_, cond)] = list(not_found.detect.conditions())
    assert s.page_contains(cond.text, cond.frame)

    set_faults(app_a, expire_session_now=True)
    s.click(cap.steps[0].action.target)
    [(_, expired)] = list(profile.session_expired.detect.conditions())
    assert s.page_contains(expired.text, expired.frame)
