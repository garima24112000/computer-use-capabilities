"""Typed perception/action data: frames, elements, observations, dialogs and action results."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Role = Literal["link", "button", "textbox", "combobox", "checkbox", "radio", "cell", "text"]
TOP_FRAME_KEY = "_top"


class FrameRef(BaseModel):
    """A frame addressed by the chain of frame `name` attributes from the top document."""

    name_path: list[str] = Field(default_factory=list)
    url_path_pattern: str | None = None  # diagnostics / fallback only, e.g. "/member/*"

    @property
    def key(self) -> str:
        """Stable string key: "_top" for the top document, else names joined by "/"."""
        return "/".join(self.name_path) or TOP_FRAME_KEY


class BBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class TableContext(BaseModel):
    row_key: str | None = None  # text of the first non-empty cell in the element's row
    column_header: str | None = None  # header-row text in the element's column
    table_caption_or_header: str | None = None  # nearest section title bar, e.g. "ACCOUNTS"


class ElementInfo(BaseModel):
    """One element in an observation. Never carries a field's value, only `has_value`."""

    ref: str | None  # ephemeral, e.g. "e12"; valid until the next observe()
    frame: FrameRef
    kind: Literal["interactive", "text"]
    role: Role
    name: str
    label: str | None = None
    tag: str
    input_type: str | None = None
    has_value: bool = False
    options: list[str] | None = None
    enabled: bool = True
    visible: bool = True
    bbox: BBox
    table_context: TableContext | None = None


class ScannedElement(ElementInfo):
    """Internal scan result: ElementInfo plus matching/acting details kept inside the surface."""

    handle: str  # value of the transient data-cua-tmp attribute
    css_path: str
    row_texts: list[str] = Field(default_factory=list)
    form_key: str | None = None  # form name (or "form#i") the control belongs to / submits
    submits_form: bool = False  # native submit control or calls <form>.submit()
    focused: bool = False  # document.activeElement of its frame

    def public(self) -> ElementInfo:
        return ElementInfo.model_validate(self.model_dump(include=set(ElementInfo.model_fields)))


class KeyContext(BaseModel):
    """What pressing a key would act on. Used to classify the risk of Enter."""

    known: bool  # False if the surface could not tell (treat as risky)
    focused: ElementInfo | None = None  # element receiving the key
    # What Enter would trigger: the focused control itself if it is a button/link, else every
    # control that submits the focused element's form. Empty = no known submit control.
    submit_controls: list[ElementInfo] = Field(default_factory=list)
    note: str = ""


class FrameInfo(BaseModel):
    frame: FrameRef
    url: str
    title: str


class DialogEvent(BaseModel):
    type: Literal["alert", "confirm", "prompt", "beforeunload"]
    message: str
    action_taken: Literal["accepted", "dismissed"]
    handled_by: str  # "policy:<name>" | "default"


class Observation(BaseModel):
    frames: list[FrameInfo]
    elements: list[ElementInfo]
    text_by_frame: dict[str, str]
    dialogs: list[DialogEvent] = Field(default_factory=list)
    taken_at: datetime

    def to_prompt_text(self) -> str:
        """Compact, deterministic rendering for an LLM: per frame, elements then visible text."""
        lines: list[str] = []
        if self.dialogs:
            lines.append("== dialogs since last observation ==")
            lines += [
                f'!! {d.type} "{d.message}" -> {d.action_taken} ({d.handled_by})'
                for d in self.dialogs
            ]
        for info in self.frames:
            key = info.frame.key
            elements = [el for el in self.elements if el.frame.key == key]
            text = self.text_by_frame.get(key, "")
            if not elements and not text:
                continue
            path = info.frame.url_path_pattern or info.url
            lines.append(f'== frame {key} {path} "{info.title}" ==')
            lines += [_element_line(el) for el in elements]
            if text:
                lines.append(f"-- text ({key}) --")
                lines.append(text)
        return "\n".join(lines)


def _element_line(el: ElementInfo) -> str:
    parts = [f'[{el.ref}] {el.role} "{el.name}"']
    if el.label and el.role not in ("link", "button"):
        parts.append(f"(label: {el.label})")
    if el.input_type and el.input_type not in ("text",):
        parts.append(f"type={el.input_type}")
    if el.options is not None:
        parts.append("options=[" + ", ".join(f'"{o}"' for o in el.options) + "]")
    if el.has_value:
        parts.append("has_value")
    if not el.enabled:
        parts.append("disabled")
    if not el.visible:
        parts.append("hidden")
    tc = el.table_context
    if tc and tc.column_header:  # only real data grids; layout tables would just add noise
        bits = []
        if tc.table_caption_or_header:
            bits.append(f'section="{tc.table_caption_or_header}"')
        if tc.row_key:
            bits.append(f'row="{tc.row_key}"')
        if tc.column_header:
            bits.append(f'col="{tc.column_header}"')
        parts.append(" ".join(bits))
    parts.append(f"frame={el.frame.key}")
    return " ".join(parts)


class StrategyAttempt(BaseModel):
    kind: str
    match_count: int
    outcome: Literal["matched", "not_found", "ambiguous", "error"]
    note: str = ""


class ResolveResult(BaseModel):
    matched: bool
    winning_strategy_index: int | None = None
    degraded: bool = False  # True if a lower-priority strategy won: a drift signal
    attempts: list[StrategyAttempt] = Field(default_factory=list)


class ElementHandleRef(BaseModel):
    """A live, short-lived handle to a resolved element. Never persist it."""

    frame: FrameRef
    selector: str  # e.g. '[data-cua-tmp="t17"]', valid only for the current document
    element: ElementInfo | None = None


class ActionResult(BaseModel):
    ok: bool
    resolve_result: ResolveResult | None = None
    dialogs: list[DialogEvent] = Field(default_factory=list)
    navigated: bool = False
    duration_ms: float
