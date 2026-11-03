"""Pure (browser-free) tests for locator matching, TargetSpec schema and prompt rendering."""

from datetime import UTC, datetime

import pytest
from pydantic import TypeAdapter

from cua.surface.locators import (
    CssPath,
    LabelAnchor,
    RoleName,
    Strategy,
    TableCell,
    TargetSpec,
    TextMatch,
    candidate_strategies,
    match,
    norm_text,
)
from cua.surface.models import (
    BBox,
    FrameInfo,
    FrameRef,
    Observation,
    ScannedElement,
    TableContext,
)

MAIN = FrameRef(name_path=["main"])
BOX = BBox(x=0, y=0, width=10, height=10)


def el(ref, role, name, kind="interactive", label=None, row=(), ctx=None, tag="td"):
    return ScannedElement(ref=ref, frame=MAIN, kind=kind, role=role, name=name, label=label,
                          tag=tag, bbox=BOX, handle=f"t{ref}", css_path=f"html > x{ref}",
                          row_texts=list(row), table_context=ctx)


ACCOUNTS = [
    el("e1", "cell", "00", "text", row=["00", "Share Savings", "$12,345.67"],
       ctx=TableContext(row_key="00", column_header="SFX", table_caption_or_header="ACCOUNTS")),
    el("e2", "cell", "$12,345.67", "text", row=["00", "Share Savings", "$12,345.67"],
       ctx=TableContext(row_key="00", column_header="BALANCE", table_caption_or_header="ACCOUNTS")),
    el("e3", "cell", "$2,500.00", "text", row=["10", "Checking", "$2,500.00"],
       ctx=TableContext(row_key="10", column_header="BALANCE", table_caption_or_header="ACCOUNTS")),
]
FORM = [
    el("e4", "text", "MEMBER NO.", "text", row=["MEMBER NO.", "", "INQUIRE"]),
    el("e5", "textbox", "MEMBER NO.", label="MEMBER NO.", row=["MEMBER NO.", "", "INQUIRE"]),
    el("e6", "link", "INQUIRE", label="MEMBER NO.", row=["MEMBER NO.", "", "INQUIRE"]),
]


def refs(found):
    return [e.ref for e in found]


@pytest.mark.parametrize("raw,expected", [
    ("  OPERATOR ID: ", "OPERATOR ID"), ("» MEMBER  INQUIRY", "MEMBER INQUIRY"),
    ("MEMBER NO.", "MEMBER NO."), ("$12,345.67", "$12,345.67"),
])
def test_norm_text(raw, expected):
    assert norm_text(raw) == expected


def test_role_name_exact_and_partial():
    assert refs(match(RoleName(role="textbox", name="MEMBER NO."), FORM)) == ["e5"]
    assert refs(match(RoleName(role="link", name="inq", exact=False), FORM)) == ["e6"]
    assert match(RoleName(role="button", name="INQUIRE"), FORM) == []


def test_label_anchor_only_matches_controls_in_labelled_row():
    assert refs(match(LabelAnchor(label="MEMBER NO.:", role="textbox"), FORM)) == ["e5"]
    assert match(LabelAnchor(label="ACCT HOLDER #", role="textbox"), FORM) == []


def test_table_cell_row_key_may_be_any_cell_in_row():
    by_desc = TableCell(section_header="ACCOUNTS", row_key="Share Savings",
                        column_header="BALANCE")
    by_sfx = TableCell(section_header="ACCOUNTS", row_key="00", column_header="BALANCE")
    assert refs(match(by_desc, ACCOUNTS)) == ["e2"] == refs(match(by_sfx, ACCOUNTS))
    assert match(TableCell(section_header="LOANS", row_key="00", column_header="BALANCE"),
                 ACCOUNTS) == []
    assert refs(match(TableCell(row_key="Checking", column_header="BALANCE"), ACCOUNTS)) == ["e3"]


def test_text_match_is_role_agnostic_by_default():
    assert refs(match(TextMatch(text="MEMBER NO."), FORM)) == ["e4", "e5"]  # ambiguous
    assert refs(match(TextMatch(text="MEMBER NO.", role="text"), FORM)) == ["e4"]


def test_css_path_is_not_matched_in_python():
    with pytest.raises(TypeError):
        match(CssPath(selector="html"), FORM)


def test_candidate_strategies_priority_order():
    kinds = [s.kind for s in candidate_strategies(ACCOUNTS[1])]
    assert kinds == ["role_name", "table_cell", "text_match", "css_path"]
    kinds = [s.kind for s in candidate_strategies(FORM[1])]
    assert kinds == ["role_name", "label_anchor", "text_match", "css_path"]


def test_target_spec_round_trips_discriminated_union():
    target = TargetSpec(frame=MAIN, description="balance", strategies=[
        RoleName(role="cell", name="$1.00"), LabelAnchor(label="X", role="textbox"),
        TableCell(row_key="00", column_header="BALANCE"), TextMatch(text="x"),
        CssPath(selector="html > body"),
    ])
    again = TargetSpec.model_validate_json(target.model_dump_json())
    assert again == target
    assert [type(s) for s in again.strategies] == [RoleName, LabelAnchor, TableCell, TextMatch,
                                                   CssPath]
    with pytest.raises(ValueError):
        TypeAdapter(Strategy).validate_python({"kind": "xpath", "selector": "//a"})


def test_prompt_text_hides_internal_fields_and_is_stable():
    obs = Observation(
        frames=[FrameInfo(frame=MAIN, url="http://x/inquiry", title="T")],
        elements=[e.public() for e in FORM + ACCOUNTS],
        text_by_frame={"main": "MEMBER NO. INQUIRE"},
        taken_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    text = obs.to_prompt_text()
    assert text == obs.to_prompt_text()
    assert '[e5] textbox "MEMBER NO." (label: MEMBER NO.) frame=main' in text
    assert 'section="ACCOUNTS" row="00" col="BALANCE"' in text
    assert "html > " not in text and "te5" not in text
    assert text.index("[e4]") < text.index("[e5]") < text.index("-- text (main) --")
