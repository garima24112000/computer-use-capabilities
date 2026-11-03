"""Tool definition and review markdown (snapshot)."""

import os
from pathlib import Path

from jsonschema import Draft202012Validator

from cua.artifact import store
from cua.artifact.profile import AppProfile
from tests.artifact.conftest import OPEN_SUBACCOUNT, READ_BALANCE

SNAPSHOT = Path(__file__).with_name("snapshots") / "read_savings_balance.md"


def test_tool_definition_matches_inputs_and_outputs():
    cap, _ = store.load(OPEN_SUBACCOUNT)
    tool = cap.to_tool_definition()
    schema = tool["input_schema"]
    Draft202012Validator.check_schema(schema)
    assert list(schema["properties"]) == [p.name for p in cap.inputs]
    assert schema["required"] == ["member_id", "account_type", "initial_deposit"]
    assert schema["properties"]["account_type"]["enum"] == ["Share Savings", "Money Market",
                                                            "Certificate 12mo"]
    assert schema["properties"]["initial_deposit"] == {
        "type": "number", "description": "Initial deposit in dollars", "minimum": 0.01,
        "maximum": 10000.0, "examples": ["250.00"]}
    validator = Draft202012Validator(schema)
    assert validator.is_valid({"member_id": "12345", "account_type": "Money Market",
                               "initial_deposit": 250})
    assert not validator.is_valid({"member_id": "abc", "account_type": "Money Market",
                                   "initial_deposit": 250})
    assert not validator.is_valid({"member_id": "12345", "account_type": "Money Market",
                                   "initial_deposit": 1, "extra": 1})
    assert tool["output_schema"]["required"] == ["confirmation_number"]
    assert tool["outcomes"][0] == "profile:member_not_found"  # no profile given
    profile = AppProfile.load_named(cap.app.profile)
    assert cap.to_tool_definition(profile)["outcomes"] == [
        "MEMBER_NOT_FOUND", "INVALID_MEMBER_FORMAT", "ACCESS_DENIED", "DEPOSIT_OUT_OF_RANGE",
        "INVALID_DEPOSIT_AMOUNT"]
    assert tool["capability"] == {"id": cap.id, "version": "1.0.0", "status": "draft"}


def test_summary_markdown_snapshot():
    cap, _ = store.load(READ_BALANCE)
    text = cap.summary_markdown()
    if os.environ.get("UPDATE_SNAPSHOTS"):
        SNAPSHOT.parent.mkdir(exist_ok=True)
        SNAPSHOT.write_text(text)
    assert text == SNAPSHOT.read_text(), "set UPDATE_SNAPSHOTS=1 to regenerate after review"
