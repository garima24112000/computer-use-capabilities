"""Happy paths, business outcomes and phase-0 failures, on both artifact sets."""

import json

import pytest

import cua.replay.session as replay_session
from tests.replay.conftest import READ, run_dir

KINDS = ["discovered", "fixture"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("member, balance", [("12345", "12345.67"), ("31415", "3141.59"),
                                             ("70001", "25.00")])
def test_read_balance_success(app_a, replay, kind, member, balance):
    r = replay(app_a, READ, {"member_id": member}, kind=kind)
    assert r.status == "success", r.failure
    assert r.outputs == {"savings_balance": balance}
    assert r.failure is None and r.outcome is None and r.irreversible_executed == []
    assert [s.status for s in r.steps] == ["ok"] * len(r.steps)
    assert r.capability.id == READ and r.capability.content_hash.startswith("sha256:")


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("member, oid, code", [
    ("99999", "member_not_found", "MEMBER_NOT_FOUND"),
    ("40300", "access_denied", "ACCESS_DENIED"),
])
def test_read_balance_business_outcomes(app_a, replay, kind, member, oid, code):
    r = replay(app_a, READ, {"member_id": member}, kind=kind)
    assert r.status == "business_outcome"
    assert (r.outcome.id, r.outcome.code) == (oid, code)
    assert r.outputs is None and r.failure is None
    assert "not_run" in [s.status for s in r.steps]  # stopped early, on purpose


@pytest.fixture
def no_browser(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a browser was launched")

    monkeypatch.setattr(replay_session, "PlaywrightSurface", boom)


@pytest.mark.parametrize("kind", KINDS)
def test_bad_input_fails_before_any_browser(app_a, replay, no_browser, kind):
    r = replay(app_a, READ, {"member_id": "abc", "extra": "x"}, kind=kind)
    assert r.status == "failed"
    f = r.failure
    assert f.category == "input_validation" and f.retryable is False and f.step_id is None
    assert "does not match" in f.observed and "unknown input 'extra'" in f.observed
    assert r.steps == []
    saved = json.loads((run_dir(r) / "result.json").read_text())
    assert saved["failure"]["category"] == "input_validation"


def test_invalid_artifact_is_a_typed_failure(app_a, replay, tmp_path, no_browser):
    root = tmp_path / "caps"
    (root / READ).mkdir(parents=True)
    (root / READ / "1.0.0.json").write_text(json.dumps({"id": READ, "version": "1.0.0",
                                                         "steps": []}))
    r = replay(app_a, READ, {"member_id": "12345"}, root=root)
    assert r.status == "failed" and r.failure.category == "artifact_invalid"
    assert r.capability.id == READ
