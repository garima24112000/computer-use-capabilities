"""Locator strategies, TargetSpec and the pure matching logic shared by describe() and resolve().

Matching runs over the same element scan that observe() produces, so record-time and replay-time
semantics are identical. Only CssPath needs the live DOM; it is matched by the surface itself.
"""

import re
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from cua.surface.models import BBox, FrameRef, Role, ScannedElement


class RoleName(BaseModel):
    kind: Literal["role_name"] = "role_name"
    role: Role
    name: str
    exact: bool = True


class LabelAnchor(BaseModel):
    """The control of `role` in the same table row as a cell whose text equals `label`."""

    kind: Literal["label_anchor"] = "label_anchor"
    label: str
    role: Role


class TableCell(BaseModel):
    """The cell in column `column_header` of the row containing a cell equal to `row_key`."""

    kind: Literal["table_cell"] = "table_cell"
    section_header: str | None = None
    row_key: str
    column_header: str


class TextMatch(BaseModel):
    kind: Literal["text_match"] = "text_match"
    text: str
    role: Role | None = None
    exact: bool = True


class CssPath(BaseModel):
    """Structural nth-of-type path. Last resort: brittle by construction."""

    kind: Literal["css_path"] = "css_path"
    selector: str
    brittle: Literal[True] = True


Strategy = Annotated[
    RoleName | LabelAnchor | TableCell | TextMatch | CssPath, Field(discriminator="kind")
]
STRATEGY_PRIORITY = ["role_name", "label_anchor", "table_cell", "text_match", "css_path"]


class Fingerprint(BaseModel):
    """What the element looked like at record time. Diagnostics only; never used for matching."""

    role: str
    name: str
    tag: str
    bbox: BBox | None = None


class TargetSpec(BaseModel):
    frame: FrameRef
    strategies: list[Strategy]
    description: str
    fingerprint: Fingerprint | None = None


_LEADING_GLYPHS = re.compile(r"^[»›•·]+\s*")


def norm_text(text: str | None) -> str:
    """Same normalization the DOM scan applies to names and cell texts."""
    text = " ".join((text or "").split())
    text = _LEADING_GLYPHS.sub("", text)
    return re.sub(r"\s*:$", "", text).strip()


def _text_eq(actual: str, wanted: str, exact: bool) -> bool:
    a, w = norm_text(actual), norm_text(wanted)
    return a == w if exact else w.lower() in a.lower()


def match(strategy: Strategy, elements: list[ScannedElement]) -> list[ScannedElement]:
    """Elements a (non-CSS) strategy selects, in document order."""
    if isinstance(strategy, RoleName):
        return [e for e in elements
                if e.role == strategy.role and _text_eq(e.name, strategy.name, strategy.exact)]
    if isinstance(strategy, LabelAnchor):
        label = norm_text(strategy.label)
        return [e for e in elements
                if e.kind == "interactive" and e.role == strategy.role
                and label in (norm_text(t) for t in e.row_texts)]
    if isinstance(strategy, TableCell):
        row_key, column = norm_text(strategy.row_key), norm_text(strategy.column_header)
        section = norm_text(strategy.section_header) if strategy.section_header else None
        out = []
        for e in elements:
            tc = e.table_context
            if tc is None or norm_text(tc.column_header) != column:
                continue
            if section is not None and norm_text(tc.table_caption_or_header) != section:
                continue
            if row_key in (norm_text(t) for t in e.row_texts):
                out.append(e)
        return out
    if isinstance(strategy, TextMatch):
        return [e for e in elements
                if (strategy.role is None or e.role == strategy.role)
                and _text_eq(e.name, strategy.text, strategy.exact)]
    raise TypeError(f"{type(strategy).__name__} is matched against the live DOM, not the scan")


def candidate_strategies(el: ScannedElement) -> list[Strategy]:
    """Every strategy that could identify `el`, in default priority order (not yet validated)."""
    out: list[Strategy] = []
    if el.name:
        out.append(RoleName(role=el.role, name=el.name))
    if el.label and el.kind == "interactive":
        out.append(LabelAnchor(label=el.label, role=el.role))
    tc = el.table_context
    if tc and tc.row_key and tc.column_header:
        out.append(TableCell(section_header=tc.table_caption_or_header, row_key=tc.row_key,
                             column_header=tc.column_header))
    if el.name:
        out.append(TextMatch(text=el.name, role=None))
    out.append(CssPath(selector=el.css_path))
    return out
