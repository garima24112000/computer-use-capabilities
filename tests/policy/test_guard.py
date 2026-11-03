"""Unit tests for PolicyGuard (pure: no browser)."""

import pytest

from cua.policy.guard import PolicyGuard
from cua.policy.models import PolicyConfig, PolicyContext

BASE = "http://127.0.0.1:54321"
REVIEW = f"{BASE}/subacct/review"


@pytest.fixture(scope="module")
def guard() -> PolicyGuard:
    return PolicyGuard(PolicyConfig.load())


def ctx(mode="discovery", url=None, **flags) -> PolicyContext:
    return PolicyContext(mode=mode, current_url=url, **flags)


@pytest.mark.parametrize("url", [f"{BASE}/__faults", f"{BASE}/__faults/reset",
                                 "http://localhost:8001/__faults"])
def test_fault_control_plane_blocked_even_on_allowed_origin(guard, url):
    d = guard.check_navigation(url, ctx())
    assert d.verdict == "block" and d.rule_id == "blocked_path"


@pytest.mark.parametrize("url,rule", [
    ("https://example.com/", "origin_not_allowlisted"),
    ("http://evil.example:8001/inquiry", "origin_not_allowlisted"),
    ("http://localhost/inquiry", "origin_not_allowlisted"),  # no port: does not match ":*"
    (f"{BASE}/admin", "path_not_allowlisted"),
    ("file:///etc/passwd", "scheme_not_allowed"),
])
def test_disallowed_urls(guard, url, rule):
    d = guard.check_navigation(url, ctx())
    assert (d.verdict, d.rule_id) == ("block", rule)


@pytest.mark.parametrize("path", ["/member/12345", "/member/detail?m=12345", "/inquiry?txtA=1",
                                  "/", "/top", "/nav", "/login", "/img/btn_submit.svg"])
def test_allowed_urls(guard, path):
    d = guard.check_navigation(BASE + path, ctx())
    assert d.verdict == "allow" and d.risk == "safe"


def test_logout_navigation_is_risky(guard):
    d = guard.check_navigation(f"{BASE}/logout", ctx())
    assert (d.verdict, d.risk, d.rule_id) == ("allow", "risky", "logout-navigation")


def test_submit_on_review_requires_approval_in_discovery(guard):
    d = guard.check_action("click", "button", "SUBMIT", ctx(url=REVIEW))
    assert (d.verdict, d.risk, d.rule_id) == ("require_approval", "irreversible", "subacct-submit")
    assert d.reason == "commits a new sub-account"


@pytest.mark.parametrize("flags,verdict", [
    ({}, "require_approval"),
    ({"artifact_approved": True}, "require_approval"),
    ({"step_approved": True}, "require_approval"),
    ({"artifact_approved": True, "step_approved": True}, "allow"),
])
def test_submit_in_replay_needs_both_approvals(guard, flags, verdict):
    d = guard.check_action("click", "button", "SUBMIT", ctx("replay", REVIEW, **flags))
    assert d.risk == "irreversible" and d.verdict == verdict


def test_submit_elsewhere_hits_generic_rule(guard):
    d = guard.check_action("click", "button", "SUBMIT", ctx(url=f"{BASE}/inquiry"))
    assert (d.risk, d.rule_id) == ("irreversible", "generic-commit-verb")


@pytest.mark.parametrize("name", ["SIGN OFF", "Log out", "SIGNOFF"])
def test_sign_off_is_risky(guard, name):
    d = guard.check_action("click", "link", name, ctx(url=f"{BASE}/top"))
    assert (d.verdict, d.risk, d.rule_id) == ("allow", "risky", "sign-off")


@pytest.mark.parametrize("name", ["CONFIRM TRANSFER", "Approve", "WIRE FUNDS", "Close Account"])
def test_unknown_commit_buttons_are_irreversible(guard, name):
    d = guard.check_action("click", "button", name, ctx(url=f"{BASE}/inquiry"))
    assert (d.verdict, d.risk, d.rule_id) == ("require_approval", "irreversible",
                                              "generic-commit-verb")


def test_defaults(guard):
    page = ctx(url=f"{BASE}/subacct/open")
    click = guard.check_action("click", "button", "CONTINUE", page)
    assert (click.risk, click.rule_id, click.verdict) == ("risky", "default:click", "allow")
    for action in ("fill", "select", "read", "press"):
        d = guard.check_action(action, "textbox", "NICKNAME", page)
        assert (d.risk, d.verdict) == ("safe", "allow")
    # The generic verb rule is scoped to clicks: filling a "CONFIRM ..." field stays safe.
    assert guard.check_action("fill", "textbox", "CONFIRM PASSWORD", page).risk == "safe"


def test_unknown_action_blocked(guard):
    d = guard.check_action("drag", "button", "X", ctx())
    assert (d.verdict, d.rule_id) == ("block", "action_not_allowed")


def test_action_on_disallowed_page_blocked(guard):
    d = guard.check_action("click", "button", "OK", ctx(url="https://example.com/x"))
    assert d.verdict == "block" and "current page not allowed" in d.reason


def _config_with(rules) -> PolicyConfig:
    data = PolicyConfig.load().model_dump()
    data["risk_rules"] = rules
    return PolicyConfig.model_validate(data)


def test_first_matching_rule_wins():
    soft = {"id": "soft", "match": {"name_regex": "(?i)submit"}, "risk": "risky"}
    hard = {"id": "hard", "match": {"name_regex": "(?i)submit"}, "risk": "irreversible"}
    c = ctx(url=f"{BASE}/inquiry")
    assert PolicyGuard(_config_with([soft, hard])).check_action(
        "click", "button", "SUBMIT", c).rule_id == "soft"
    assert PolicyGuard(_config_with([hard, soft])).check_action(
        "click", "button", "SUBMIT", c).rule_id == "hard"


def test_rule_match_fields_are_anded():
    rule = {"id": "r", "match": {"action": ["click"], "role": "link", "url_glob": "/top"},
            "risk": "safe"}
    g = PolicyGuard(_config_with([rule]))
    assert g.check_action("click", "link", "X", ctx(url=f"{BASE}/top")).rule_id == "r"
    assert g.check_action("click", "button", "X", ctx(url=f"{BASE}/top")).rule_id != "r"
    assert g.check_action("click", "link", "X", ctx(url=f"{BASE}/nav")).rule_id != "r"


def test_invalid_regex_rejected_at_load():
    with pytest.raises(ValueError):
        _config_with([{"id": "bad", "match": {"name_regex": "("}, "risk": "safe"}])
