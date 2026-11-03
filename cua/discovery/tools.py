"""The agent's action space: tool JSON Schemas and validation before anything executes.

The model points at elements by `ref` from the CURRENT observation only. It can type literals
or `{{inputs.<name>}}` placeholders. Secrets are never available to it: sign-on is done
deterministically by the profile's login subflow before the loop starts. There is no navigate
tool and no JavaScript tool.
"""

from typing import Any, Literal

from pydantic import BaseModel

from cua.discovery.llm import ToolCall, ToolSpec
from cua.surface.models import Observation
from cua.templating import TemplateError, placeholders

ALLOWED_KEYS = ("Enter", "Tab", "Escape")
ActionName = Literal["click", "fill", "select", "press", "extract"]
_INTENT = {"type": "string", "minLength": 3,
           "description": "One sentence: why this action moves toward the goal."}
_REF = {"type": "string", "pattern": "^e[0-9]+$",
        "description": "Element ref from the LATEST observation, e.g. e12."}


def _spec(name: str, description: str, props: dict[str, Any], required: list[str]) -> ToolSpec:
    return ToolSpec(name=name, description=description, input_schema={
        "type": "object", "properties": props, "required": required,
        "additionalProperties": False})


TOOLS: list[ToolSpec] = [
    _spec("click", "Click a link, button or clickable cell.",
          {"ref": _REF, "intent": _INTENT}, ["ref", "intent"]),
    _spec("fill", "Replace the text of an input field. Use {{inputs.<name>}} for declared "
                  "inputs instead of typing their values.",
          {"ref": _REF, "value": {"type": "string"}, "intent": _INTENT},
          ["ref", "value", "intent"]),
    _spec("select", "Choose an option (by its visible label) in a dropdown.",
          {"ref": _REF, "option": {"type": "string"}, "intent": _INTENT},
          ["ref", "option", "intent"]),
    _spec("press", "Press a key, optionally on an element. Allowed keys: "
                   + ", ".join(ALLOWED_KEYS) + ".",
          {"key": {"type": "string", "enum": list(ALLOWED_KEYS)}, "ref": _REF,
           "intent": _INTENT}, ["key", "intent"]),
    _spec("extract", "Read the text of an element into a declared output.",
          {"ref": _REF, "output": {"type": "string"}, "intent": _INTENT},
          ["ref", "output", "intent"]),
    _spec("done", "Finish: every output has been extracted, or the app returned a business "
                  "outcome (e.g. NO RECORDS FOUND). Summarize what happened.",
          {"summary": {"type": "string", "minLength": 3}}, ["summary"]),
    _spec("escalate", "Stop and ask a human operator for help when you are blocked.",
          {"reason": {"type": "string", "minLength": 3}}, ["reason"]),
]
TOOL_NAMES = {t.name for t in TOOLS}


class ValidatedCall(BaseModel):
    """A tool call that passed validation and may be executed."""

    call_id: str
    name: Literal["click", "fill", "select", "press", "extract", "done", "escalate"]
    intent: str = ""
    ref: str | None = None
    value: str | None = None  # fill value / select option, still templated
    key: str | None = None
    output: str | None = None
    text: str = ""  # done summary / escalate reason

    @property
    def is_action(self) -> bool:
        return self.name in ("click", "fill", "select", "press", "extract")


class ToolValidationError(ValueError):
    pass


def _template(value: Any, inputs: set[str], field: str) -> str:
    if not isinstance(value, str):
        raise ToolValidationError(f"{field} must be a string")
    try:
        found = placeholders(value)
    except TemplateError as exc:
        raise ToolValidationError(f"{field}: {exc}") from None
    for p in found:
        if p.kind == "secret":
            raise ToolValidationError("secrets are not available to the agent; sign-on is "
                                      "already done")
        if p.name not in inputs:
            raise ToolValidationError(f"{field}: {p.raw} is not a declared input "
                                      f"(declared: {sorted(inputs) or 'none'})")
    return value


def validate_call(call: ToolCall, obs: Observation, inputs: set[str], outputs: set[str]
                  ) -> ValidatedCall:
    """Validate against the CURRENT observation and the goal contract; raise on any problem."""
    if call.name not in TOOL_NAMES:
        raise ToolValidationError(f"unknown tool {call.name!r}")
    args = call.args or {}
    spec = next(t for t in TOOLS if t.name == call.name)
    allowed = set(spec.input_schema["properties"])
    if extra := set(args) - allowed:
        raise ToolValidationError(f"unexpected arguments {sorted(extra)}")
    if missing := [r for r in spec.input_schema["required"] if not args.get(r)]:
        raise ToolValidationError(f"missing required arguments {missing}")
    if call.name in ("done", "escalate"):
        text = args.get("summary") if call.name == "done" else args.get("reason")
        return ValidatedCall(call_id=call.id, name=call.name, text=str(text))

    refs = {e.ref for e in obs.elements if e.ref}
    ref = args.get("ref")
    if ref is not None and ref not in refs:
        raise ToolValidationError(f"ref {ref!r} is not in the latest observation")
    out = ValidatedCall(call_id=call.id, name=call.name, intent=str(args["intent"]), ref=ref)
    if call.name == "fill":
        out.value = _template(args["value"], inputs, "value")
    elif call.name == "select":
        out.value = _template(args["option"], inputs, "option")
        el = next(e for e in obs.elements if e.ref == ref)
        if el.options is not None and out.value not in el.options \
                and not placeholders(out.value):
            raise ToolValidationError(f"option {out.value!r} not in {el.options}")
    elif call.name == "press":
        if args["key"] not in ALLOWED_KEYS:
            raise ToolValidationError(f"key {args['key']!r} not allowed ({ALLOWED_KEYS})")
        out.key = args["key"]
    elif call.name == "extract":
        if args["output"] not in outputs:
            raise ToolValidationError(f"output {args['output']!r} is not declared "
                                      f"(declared: {sorted(outputs)})")
        out.output = args["output"]
    return out
