"""The value-template grammar shared by artifacts and locators. It is deliberately tiny.

A template is a string made of literal text plus placeholders of exactly two shapes:

    {{inputs.<snake_case_name>}}   a declared capability input, substituted at replay time
    {{secret:<ENV_NAME>}}          a secret, resolved by GuardedSurface at fill time

A secret placeholder must be the whole value. GuardedSurface resolves and logs it as one unit,
so it is never concatenated with other text. There are no expressions, filters, defaults,
environment lookups or nesting. Anything else between braces (`{{ 1+1 }}`, `{{env.X}}`,
`{{inputs.a|upper}}`) is rejected, and so are stray `{{` or `}}`.
"""

import re
from collections.abc import Callable, Mapping
from typing import Any, Literal

from pydantic import BaseModel

INPUT_NAME = r"[a-z][a-z0-9_]*"
SECRET_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_INPUT_BODY = re.compile(rf"^inputs\.({INPUT_NAME})$")
_SECRET_BODY = re.compile(rf"^secret:({SECRET_NAME})$")
INPUT_TEMPLATE = re.compile(rf"^\{{\{{inputs\.({INPUT_NAME})\}}\}}$")


class Placeholder(BaseModel):
    kind: Literal["input", "secret"]
    name: str
    raw: str  # the full "{{...}}" text


class TemplateError(ValueError):
    pass


def placeholders(template: str) -> list[Placeholder]:
    """Every placeholder in `template`, in order. Raises TemplateError on anything off-grammar."""
    out: list[Placeholder] = []
    for m in _PLACEHOLDER.finditer(template):
        body = m.group(1)
        if mi := _INPUT_BODY.match(body):
            out.append(Placeholder(kind="input", name=mi.group(1), raw=m.group(0)))
        elif ms := _SECRET_BODY.match(body):
            out.append(Placeholder(kind="secret", name=ms.group(1), raw=m.group(0)))
        else:
            raise TemplateError(
                f"unsupported placeholder {m.group(0)!r}: only {{{{inputs.<name>}}}} and "
                f"{{{{secret:<NAME>}}}} are allowed (no expressions)")
    leftover = _PLACEHOLDER.sub("", template)
    if "{{" in leftover or "}}" in leftover:
        raise TemplateError(f"unbalanced template braces in {template!r}")
    secrets = [p for p in out if p.kind == "secret"]
    if secrets and template != secrets[0].raw:
        raise TemplateError(
            f"a secret placeholder must be the entire value, got {template!r}")
    return out


def validate_template(template: str) -> str:
    placeholders(template)
    return template


def validate_input_template(template: str) -> str:
    """Like validate_template, but secrets are not allowed (texts that are matched or shown)."""
    if any(p.kind == "secret" for p in placeholders(template)):
        raise TemplateError(f"secret placeholders are not allowed here: {template!r}")
    return template


def input_names(template: str) -> list[str]:
    return [p.name for p in placeholders(template) if p.kind == "input"]


def is_secret_template(template: str) -> bool:
    ps = placeholders(template)
    return len(ps) == 1 and ps[0].kind == "secret"


def render(template: str, inputs: Mapping[str, Any],
           secret_resolver: Callable[[str], str] | None = None) -> str:
    """Substitute placeholders.

    Input values are str()-ed. A missing input is rendered as "", because required inputs are
    enforced by the contract before replay. Secret placeholders are kept verbatim when no
    resolver is given. That is the normal replay path: GuardedSurface.fill resolves them, so
    the plaintext never passes through the replay engine.
    """
    out = template
    for p in placeholders(template):
        if p.kind == "input":
            value = inputs.get(p.name)
            out = out.replace(p.raw, "" if value is None else str(value), 1)
        elif secret_resolver is not None:
            out = out.replace(p.raw, secret_resolver(p.raw), 1)
    return out
