"""The value-template grammar and parameterized TableCell row keys."""

import pytest

from cua.surface.locators import RoleName, TableCell, TargetSpec, match
from cua.surface.models import FrameRef
from cua.templating import TemplateError, input_names, placeholders, render


@pytest.mark.parametrize("bad", [
    "{{ 1+1 }}", "{{env.HOME}}", "{{inputs.a|upper}}", "{{inputs.Bad}}", "{{inputs}}",
    "{{secret:}}", "{{inputs.a}", "x }} y", "{{ inputs.a }}", "{{inputs.a.b}}",
    "pre {{secret:PW}}",  # a secret must be the whole value
])
def test_grammar_rejects_everything_but_inputs_and_secrets(bad):
    with pytest.raises(TemplateError):
        placeholders(bad)


def test_placeholders_and_render():
    t = "MEMBER DETAIL - {{inputs.member_id}} / {{inputs.nick}}"
    assert input_names(t) == ["member_id", "nick"]
    assert render(t, {"member_id": "12345"}) == "MEMBER DETAIL - 12345 / "
    assert render("plain", {}) == "plain"


def test_secret_kept_verbatim_unless_resolver_given():
    t = "{{secret:BANK_OPERATOR_PASS}}"
    assert [p.kind for p in placeholders(t)] == ["secret"]
    assert render(t, {}) == t  # replay passes the template on to GuardedSurface.fill
    assert render(t, {}, secret_resolver=lambda raw: "resolved") == "resolved"


def test_table_cell_row_key_template_binds_and_refuses_unbound_match():
    cell = TableCell(row_key="{{inputs.member_id}}", column_header="NAME")
    assert cell.row_key_input == "member_id"
    assert TableCell(row_key="Share Savings", column_header="BALANCE").row_key_input is None
    spec = TargetSpec(frame=FrameRef(name_path=["main"]), description="row",
                      strategies=[cell, RoleName(role="link", name="x")])
    assert spec.input_names == ["member_id"]
    bound = spec.bind({"member_id": "12345"})
    assert bound.strategies[0].row_key == "12345" and spec.strategies[0].row_key_input
    with pytest.raises(ValueError, match="no value"):
        spec.bind({})
    with pytest.raises(ValueError, match="unbound row_key"):
        match(cell, [])
