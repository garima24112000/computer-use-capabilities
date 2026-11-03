"""Serialization, content hashing, the approve flow and JSON Schema export."""

import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from typer.testing import CliRunner

from cua.artifact import store
from cua.cli import app
from tests.artifact.conftest import OPEN_SUBACCOUNT, READ_BALANCE, validate

runner = CliRunner()


def test_fixtures_on_disk_are_canonical():
    for path in (READ_BALANCE, OPEN_SUBACCOUNT):
        cap, _ = store.load(path)
        assert store.canonical_json(cap) == path.read_text(encoding="utf-8")


def test_round_trip_is_byte_identical(tmp_path):
    cap, _ = store.load(READ_BALANCE)
    first = store.save(cap, root=tmp_path)
    assert first == tmp_path / cap.id / "1.0.0.json"
    blob = first.read_bytes()
    again, _ = store.load(first)
    second = store.save(again, root=tmp_path / "b")
    assert second.read_bytes() == blob
    assert blob.endswith(b"\n") and blob.startswith(b'{\n  "app"')  # sorted, 2-space indent


def test_save_refuses_functional_overwrite_but_allows_same_content(tmp_path):
    cap, _ = store.load(READ_BALANCE)
    path = store.save(cap, root=tmp_path)
    store.save(cap, root=tmp_path)  # identical: no-op
    edited = cap.model_copy(update={"title": "Read savings (edited)"})
    with pytest.raises(store.ArtifactExists, match="bump the version"):
        store.save(edited, root=tmp_path)
    store.save(edited.model_copy(update={"version": "1.1.0"}), root=tmp_path)
    assert path.read_text() == store.canonical_json(cap)


def test_content_hash_ignores_approval_but_not_edits(tmp_path):
    cap, _ = store.load(OPEN_SUBACCOUNT)
    h = cap.content_hash()
    assert h.startswith("sha256:") and len(h) == 71
    path = tmp_path / "open.json"
    path.write_text(OPEN_SUBACCOUNT.read_text())
    assert runner.invoke(app, ["artifact", "approve", str(path), "--reviewer", "ops"]
                         ).exit_code == 0
    approved, _ = store.load(path)
    assert approved.content_hash() == h and approved.is_approved()

    data = json.loads(path.read_text())
    data["steps"][5]["action"]["target"]["strategies"][0]["name"] = "CONTINUE >>"
    edited = validate(data)
    assert edited.content_hash() != h
    assert not edited.is_approved()  # tamper/edit detection
    assert "approval_stale" in edited.lint().codes()
    data["steps"][6]["intent"] = "changed intent"
    assert validate(data).content_hash() not in (h, edited.content_hash())


def test_draft_with_unapproved_irreversible_step_needs_the_approve_command(tmp_path):
    cap, _ = store.load(OPEN_SUBACCOUNT)
    with pytest.raises(ValidationError, match="approval_missing"):
        validate({**cap.model_dump(mode="json", exclude_none=True), "status": "approved",
                  "review": {"reviewed_by": "ops", "approved_at": "2026-11-03T00:00:00Z",
                             "approved_content_hash": cap.content_hash()}})

    path = tmp_path / "open.json"
    path.write_text(OPEN_SUBACCOUNT.read_text())
    result = runner.invoke(app, ["artifact", "approve", str(path), "--reviewer", "ops lead",
                                 "--note", "reviewed; contact ops@example.com"])
    assert result.exit_code == 0, result.output
    assert "approved step s07_submit (irreversible)" in result.output
    approved, report = store.load(path)
    assert approved.status == "approved" and report.ok
    assert [s.id for s in approved.steps if s.approved] == ["s07_submit"]
    assert approved.review.reviewed_by == "ops lead" and approved.review.approved_at
    assert approved.review.notes == "reviewed; contact [REDACTED:EMAIL]"
    assert approved.review.approved_content_hash == cap.content_hash()
    assert runner.invoke(app, ["artifact", "approve", str(path), "--reviewer", "a;b"]
                         ).exit_code == 2


def test_validate_show_tool_cli(tmp_path):
    ok = runner.invoke(app, ["artifact", "validate", str(READ_BALANCE)])
    assert ok.exit_code == 0 and "OK meridian.member.read_savings_balance v1.0.0" in ok.output
    bad = tmp_path / "bad.json"
    data = json.loads(READ_BALANCE.read_text())
    data["steps"][1]["action"]["value"] = "{{inputs.nope}}"
    bad.write_text(json.dumps(data))
    res = runner.invoke(app, ["artifact", "validate", str(bad)])
    assert res.exit_code == 1 and "undeclared_input" in res.output
    assert runner.invoke(app, ["artifact", "show", str(READ_BALANCE)]).output.startswith("# ")
    tool = json.loads(runner.invoke(app, ["artifact", "tool", str(READ_BALANCE)]).output)
    assert tool["name"] == "meridian_member_read_savings_balance"


def test_json_schema_export_is_valid_and_fixtures_conform(tmp_path):
    paths = store.export_schemas(tmp_path)
    assert [p.name for p in paths] == ["capability.schema.json", "app_profile.schema.json"]
    cap_schema = json.loads(paths[0].read_text())
    Draft202012Validator.check_schema(cap_schema)
    Draft202012Validator.check_schema(json.loads(paths[1].read_text()))
    for fixture in (READ_BALANCE, OPEN_SUBACCOUNT):
        Draft202012Validator(cap_schema).validate(json.loads(fixture.read_text()))
    committed = store.SCHEMAS_DIR / "capability.schema.json"
    assert committed.read_text() == paths[0].read_text(), "run `cua artifact schema`"
