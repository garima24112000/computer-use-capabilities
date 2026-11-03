"""Inputs, outputs and business outcomes."""

import pytest
from pydantic import ValidationError

from cua.artifact.contract import InputParam, OutputField, money_to_decimal
from tests.artifact.conftest import OPEN_SUBACCOUNT, READ_BALANCE, validate
from tests.artifact.conftest import raw as load_raw

MEMBER = InputParam(name="member_id", type="string", description="m", pattern=r"^\d{5}$")


def codes(errors) -> list[str]:
    return [e.code for e in errors]


def test_member_id_validation_returns_typed_errors():
    assert MEMBER.validate_value("12345") == []
    [err] = MEMBER.validate_value("abc")
    assert (err.input, err.code) == ("member_id", "pattern_mismatch")
    assert "'abc'" in err.message
    assert codes(MEMBER.validate_value(12345)) == ["wrong_type"]
    assert codes(MEMBER.validate_value(None)) == ["missing"]


def test_decimal_enum_integer_date_rules():
    deposit = InputParam(name="d", type="decimal", description="d", min="0.01", max="10000")
    assert deposit.validate_value("250.00") == [] and deposit.validate_value(250) == []
    assert codes(deposit.validate_value("0")) == ["below_min"]
    assert codes(deposit.validate_value(10000.01)) == ["above_max"]
    assert codes(deposit.validate_value("ten")) == ["wrong_type"]
    assert codes(deposit.validate_value("NaN")) == ["wrong_type"]
    kind = InputParam(name="k", type="enum", description="k", enum_values=["A", "B"])
    assert codes(kind.validate_value("C")) == ["not_in_enum"]
    n = InputParam(name="n", type="integer", description="n", max=3)
    assert n.validate_value("3") == [] and codes(n.validate_value(True)) == ["wrong_type"]
    assert codes(n.validate_value(4)) == ["above_max"]
    d = InputParam(name="d", type="date", description="d")
    assert d.validate_value("2026-11-03") == [] and codes(d.validate_value("11/03")) == [
        "invalid_date"]
    optional = InputParam(name="o", type="string", description="o", required=False)
    assert optional.validate_value(None) == []


def test_sensitive_input_never_echoes_value_and_cannot_have_example():
    ssn = InputParam(name="ssn", type="string", description="s", pattern=r"^\d{3}-\d{2}-\d{4}$",
                     sensitive=True)
    [err] = ssn.validate_value("900-12-345")
    assert "900-12-345" not in err.message and "<sensitive>" in err.message
    with pytest.raises(ValidationError, match="must not carry an example"):
        InputParam(name="ssn", type="string", description="s", sensitive=True,
                   example="x")


@pytest.mark.parametrize("kwargs, msg", [
    ({"type": "enum"}, "enum_values"),
    ({"type": "string", "min": 1}, "min/max"),
    ({"type": "integer", "min": 5, "max": 1}, "min > max"),
    ({"type": "string", "pattern": "("}, "invalid regex"),
    ({"type": "string", "pattern": r"^\d+$", "example": "abc"}, "example is invalid"),
])
def test_input_param_consistency(kwargs, msg):
    with pytest.raises(ValidationError, match=msg):
        InputParam(name="x", description="x", **kwargs)


def test_capability_validate_inputs():
    cap = validate(load_raw(OPEN_SUBACCOUNT))
    ok = {"member_id": "12345", "account_type": "Money Market", "initial_deposit": "250.00"}
    assert cap.validate_inputs(ok) == []
    errs = cap.validate_inputs({"member_id": "abc", "account_type": "Gold",
                                "initial_deposit": "0", "bogus": 1})
    assert sorted(codes(errs)) == ["below_min", "not_in_enum", "pattern_mismatch",
                                   "unknown_input"]


def test_output_normalization():
    bal = OutputField(name="b", type="money", description="b", normalize="money_to_decimal")
    assert bal.normalize_value("$12,345.67") == "12345.67"
    assert money_to_decimal("-$1.00") == "-1.00" and money_to_decimal("($2.50)") == "-2.50"
    with pytest.raises(ValueError):
        bal.normalize_value("n/a")
    with pytest.raises(ValueError, match="empty"):
        bal.normalize_value("  ")
    conf = validate(load_raw(OPEN_SUBACCOUNT)).outputs[0]
    assert conf.normalize_value(
        "SUB-ACCOUNT OPENED — CONFIRMATION NO. MC-004211") == "MC-004211"
    digits = OutputField(name="d", type="string", description="d", normalize="digits_only",
                         nullable=True)
    assert digits.normalize_value("MC-004211") == "004211" and digits.normalize_value("") is None
    with pytest.raises(ValidationError, match="exactly one capture group"):
        OutputField(name="x", type="string", description="x", extract_pattern="MC-\\d+")


def test_outcomes_inherited_by_reference():
    cap = validate(load_raw(READ_BALANCE))
    assert cap.use_profile_outcomes == ["member_not_found", "invalid_member_format",
                                        "access_denied"]
