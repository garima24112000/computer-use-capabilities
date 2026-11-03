"""Fixture artifacts (hand-written, see README) and helpers to mutate them in lint tests."""

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.artifact.schema import Capability
from cua.artifact.store import profile_labels

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "capabilities"
READ_BALANCE = FIXTURES / "meridian.member.read_savings_balance" / "1.0.0.json"
OPEN_SUBACCOUNT = FIXTURES / "meridian.subaccount.open" / "1.0.0.json"
LABELS = profile_labels("meridian-core")


def raw(path: Path = READ_BALANCE) -> dict:
    return copy.deepcopy(json.loads(path.read_text()))


def validate(data: dict) -> Capability:
    return Capability.model_validate(data, context={"sensitive_labels": LABELS})


def lint_errors(data: dict) -> str:
    """The ValidationError text for data that must fail lint."""
    with pytest.raises(ValidationError) as exc:
        validate(data)
    return str(exc.value)


@pytest.fixture
def read_raw() -> dict:
    return raw(READ_BALANCE)


@pytest.fixture
def open_raw() -> dict:
    return raw(OPEN_SUBACCOUNT)
