"""Unit tests for the Redactor, SecretResolver and mask target generation."""

from datetime import UTC, datetime

import pytest

from cua.policy.errors import SecretMissing
from cua.policy.models import PolicyConfig
from cua.policy.redaction import Redactor, SecretResolver, sensitive_mask_targets
from cua.surface.models import BBox, ElementInfo, FrameInfo, FrameRef, Observation, TableContext

LABELS = PolicyConfig.load().sensitive_labels
MAIN = FrameRef(name_path=["main"])
BOX = BBox(x=0, y=0, width=1, height=1)


@pytest.fixture
def r() -> Redactor:
    return Redactor(LABELS)


@pytest.mark.parametrize("raw,expected", [
    ("SSN 900-12-3456.", "SSN [REDACTED:SSN]."),
    ("card 4111 1111 1111 1111 ok", "card [REDACTED:CARD] ok"),
    ("card 4111-1111-1111-1111", "card [REDACTED:CARD]"),
    ("amex 378282246310005", "amex [REDACTED:CARD]"),
    ("call (555) 555-0101", "call [REDACTED:PHONE]"),
    ("call 555-555-0101 / 555.555.0101", "call [REDACTED:PHONE] / [REDACTED:PHONE]"),
    ("call +1 555-555-0101", "call [REDACTED:PHONE]"),
    ("mail john.sample@example.com now", "mail [REDACTED:EMAIL] now"),
    ("acct 123456789", "acct [REDACTED:ACCOUNT_NUMBER]"),
])
def test_patterns(r, raw, expected):
    assert r.redact_text(raw) == expected


@pytest.mark.parametrize("raw", [
    "$12,345.67", "member 12345", "MEMBER NO. 99999", "MC-491934", "12345678",
    "2026-11-03T01:02:03.123456+00:00", "20261103T010203Z-ab12cd", "SFX 00 / 10",
    "1234 5678 9012 3456",  # Luhn-invalid, and space-separated so not a digit run either
])
def test_non_sensitive_values_survive(r, raw):
    assert r.redact_text(raw) == raw


def test_luhn_invalid_sixteen_digits_is_not_a_card(r):
    out = r.redact_text("1234567812345678")
    assert "CARD" not in out
    assert out == "[REDACTED:ACCOUNT_NUMBER]"  # still caught as a long digit run


def test_exact_values_and_short_values(r):
    r.register_secret("demo-pass-123")
    r.register_sensitive("abc", "NAME")  # < 4 chars: ignored to avoid shredding common text
    assert r.redact_text("pw=demo-pass-123; abc") == "pw=[REDACTED:SECRET]; abc"


def test_redact_is_recursive_and_returns_copies(r):
    r.register_sensitive("SAMPLE, JOHN Q", "NAME")
    data = {"a": ["900-12-3456", {"b": ("SAMPLE, JOHN Q", 7, None)}], "n": 1.5}
    out = r.redact(data)
    assert out == {"a": ["[REDACTED:SSN]", {"b": ("[REDACTED:NAME]", 7, None)}], "n": 1.5}
    assert data["a"][0] == "900-12-3456"


def el(ref, name, role="cell", kind="text", row=None, col=None, section=None, label=None):
    tc = TableContext(row_key=row, column_header=col, table_caption_or_header=section)
    return ElementInfo(ref=ref, frame=MAIN, kind=kind, role=role, name=name, label=label,
                       tag="td", bbox=BOX, table_context=tc)


def detail_observation() -> Observation:
    info = [("MEMBER NAME", "SAMPLE, JOHN Q"), ("SSN", "900-12-3456"),
            ("ADDRESS", "100 TEST LANE, SPRINGFIELD, ZZ 00001"), ("PHONE", "(555) 555-0101"),
            ("EMAIL", "john.sample@example.com"), ("BRANCH", "001 - MAIN OFFICE")]
    elements = []
    for i, (label, value) in enumerate(info):
        elements.append(el(f"e{2 * i + 1}", label, row=label, section="MEMBER DETAIL - 12345"))
        elements.append(el(f"e{2 * i + 2}", value, row=label, section="MEMBER DETAIL - 12345"))
    elements += [
        el("e20", "$12,345.67", row="00", col="BALANCE", section="ACCOUNTS"),
        el("e21", "Share Savings", row="00", col="DESCRIPTION", section="ACCOUNTS"),
        el("e22", "NICKNAME", role="textbox", kind="interactive", row="NICKNAME",
           label="NICKNAME"),
        el("e23", "PASSWORD", role="textbox", kind="interactive", row="PASSWORD",
           label="PASSWORD"),
    ]
    text = " ".join(f"{k} {v}" for k, v in info) + " ACCOUNTS 00 Share Savings $12,345.67"
    return Observation(frames=[FrameInfo(frame=MAIN, url="http://x/member/detail?m=12345",
                                         title="DETAIL")],
                       elements=elements, text_by_frame={"main": text},
                       taken_at=datetime(2026, 1, 1, tzinfo=UTC))


def test_structural_redaction_of_member_detail(r):
    obs = detail_observation()
    out = r.redact_observation(obs, LABELS)
    names = {e.ref: e.name for e in out.elements}
    assert names["e2"] == "[REDACTED:NAME]"
    assert names["e4"] == "[REDACTED:SSN]"
    assert names["e6"] == "[REDACTED:ADDRESS]"
    assert names["e8"] == "[REDACTED:PHONE]"
    assert names["e10"] == "[REDACTED:EMAIL]"
    assert names["e12"] == "001 - MAIN OFFICE"
    # labels themselves, money, and form-field names are kept
    assert names["e1"] == "MEMBER NAME" and names["e3"] == "SSN"
    assert names["e20"] == "$12,345.67" and names["e22"] == "NICKNAME"
    assert names["e23"] == "PASSWORD"
    dumped = out.model_dump_json()
    for raw in ("SAMPLE, JOHN Q", "900-12-3456", "100 TEST LANE", "555-0101",
                "john.sample@example.com"):
        assert raw not in dumped
    assert "$12,345.67" in out.text_by_frame["main"]
    assert obs.elements[1].name == "SAMPLE, JOHN Q"  # input untouched


def test_values_learned_structurally_are_redacted_everywhere(r):
    r.redact_observation(detail_observation(), LABELS)
    assert r.redact_text("Welcome back SAMPLE, JOHN Q") == "Welcome back [REDACTED:NAME]"


def test_nickname_is_not_matched_by_name_label(r):
    obs = detail_observation()
    obs.elements.append(el("e30", "RAINY DAY", row="NICKNAME"))
    out = r.redact_observation(obs, LABELS)
    assert next(e for e in out.elements if e.ref == "e30").name == "RAINY DAY"


def test_mask_targets_cover_sensitive_cells_without_leaking_in_descriptions():
    targets = sensitive_mask_targets(detail_observation(), LABELS)
    assert len(targets) == 5
    for t in targets:
        assert t.frame == MAIN
        assert "900-12-3456" not in t.description and "SAMPLE" not in t.description


def test_secret_resolver_registers_before_returning(r):
    resolver = SecretResolver(r, env={"BANK_OPERATOR_PASS": "demo-pass-123"})
    assert resolver.is_template("{{secret:BANK_OPERATOR_PASS}}")
    assert not resolver.is_template("demo-pass-123")
    assert resolver.resolve("{{secret:BANK_OPERATOR_PASS}}") == "demo-pass-123"
    assert r.redact_text("x demo-pass-123 y") == "x [REDACTED:SECRET] y"
    with pytest.raises(SecretMissing):
        resolver.resolve("{{secret:NOPE}}")
    with pytest.raises(ValueError):
        resolver.resolve("plain")
