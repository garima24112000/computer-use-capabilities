"""Redaction of secrets and PII before anything is logged, persisted or shown to the model.

Layers, applied in order:
1. Exact known values: secrets (resolved `{{secret:NAME}}`) and sensitive values discovered at
   runtime (typed into sensitive fields, or read from cells labelled SSN/ADDRESS/...).
2. Regex patterns: SSN, Luhn-valid card numbers, US phone numbers, email, long digit runs.
3. Structural (observations only): cells whose row label / column header is a sensitive label.
   Their values are registered into layer 1 so they are also scrubbed from page text and logs.

Deliberately NOT redacted: money amounts ("$12,345.67") and 5-digit member numbers. Member
numbers are the business identifier the flows need; treating them as non-sensitive is a
documented trade-off for this demo app, and a production policy would revisit it.
"""

import os
import re
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from cua.policy.errors import SecretMissing
from cua.surface.locators import RoleName, TableCell, TargetSpec, norm_text
from cua.surface.models import ElementInfo, Observation

MIN_EXACT_LEN = 4
SECRET_TEMPLATE = re.compile(r"^\{\{secret:([A-Za-z_][A-Za-z0-9_]*)\}\}$")
LABEL_KIND_ALIASES = {
    "SOCIAL_SECURITY": "SSN", "DATE_OF_BIRTH": "DOB", "MEMBER_NAME": "NAME",
    "OPERATOR_PASSWORD": "PASSWORD",
}


def token(kind: str) -> str:
    return f"[REDACTED:{kind}]"


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _card(m: re.Match) -> str:
    digits = re.sub(r"[ -]", "", m.group(0))
    return token("CARD") if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)


# (kind, pattern, replacement). Order matters: specific shapes before generic digit runs.
PATTERNS: list[tuple[str, re.Pattern, Any]] = [
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), token("SSN")),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), token("EMAIL")),
    ("CARD", re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])"), _card),
    ("PHONE", re.compile(r"(?<![\d\w])(?:\+?1[-.\s]?)?(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.]\d{4}"
                         r"(?!\d)"), token("PHONE")),
    ("ACCOUNT_NUMBER", re.compile(r"(?<!\d)\d{9,}(?!\d)"), token("ACCOUNT_NUMBER")),
]


class SensitiveLabels:
    """Case-insensitive whole-word matcher for sensitive labels ("NAME" hits "MEMBER NAME",
    not "NICKNAME")."""

    def __init__(self, labels: list[str]):
        ordered = sorted({norm_text(lab).upper() for lab in labels if lab.strip()},
                         key=len, reverse=True)
        self._res = [(lab, re.compile(rf"(?<![A-Z0-9]){re.escape(lab)}(?![A-Z0-9])"))
                     for lab in ordered]

    def kind_for(self, text: str | None) -> str | None:
        upper = norm_text(text).upper()
        for label, rx in self._res:
            if rx.search(upper):
                kind = label.replace(" ", "_")
                return LABEL_KIND_ALIASES.get(kind, kind)
        return None


def element_sensitivity(el: ElementInfo, labels: SensitiveLabels) -> str | None:
    """Kind of PII an element's *name* carries, or None. Form controls are never sensitive by
    name: their name is a label ("PASSWORD"), not a value."""
    if el.kind == "interactive" and el.role not in ("link", "button"):
        return None
    tc = el.table_context
    if tc is None or not el.name:
        return None
    if tc.column_header and (kind := labels.kind_for(tc.column_header)):
        return kind
    if tc.row_key and norm_text(el.name) != norm_text(tc.row_key):
        return labels.kind_for(tc.row_key)
    return None


class Redactor:
    def __init__(self, sensitive_labels: list[str] | None = None):
        self.labels = SensitiveLabels(sensitive_labels or [])
        self._exact: dict[str, str] = {}  # value -> kind

    # --- layer 1 --------------------------------------------------------------------------------

    def register_secret(self, value: str, kind: str = "SECRET") -> None:
        """Secrets are also scrubbed in upper/lower case: UIs echo ids re-cased (DEMO_OPERATOR)."""
        for variant in {value, value.upper(), value.lower()}:
            self._register(variant, kind)

    def register_sensitive(self, value: str, kind: str) -> None:
        self._register(value, kind)

    def _register(self, value: str, kind: str) -> None:
        value = value.strip()
        if len(value) >= MIN_EXACT_LEN:
            self._exact.setdefault(value, kind)
            collapsed = " ".join(value.split())
            if collapsed != value and len(collapsed) >= MIN_EXACT_LEN:
                self._exact.setdefault(collapsed, kind)

    # --- layers 1+2 -----------------------------------------------------------------------------

    def redact_text(self, text: str) -> str:
        for value in sorted(self._exact, key=len, reverse=True):
            if value in text:
                text = text.replace(value, token(self._exact[value]))
        for _, pattern, repl in PATTERNS:
            text = pattern.sub(repl, text)
        return text

    def redact(self, obj: Any) -> Any:
        """Redacted copy of str / dict / list / tuple / pydantic model (recursively)."""
        if isinstance(obj, str):
            return self.redact_text(obj)
        if isinstance(obj, BaseModel):
            return type(obj).model_validate(self.redact(obj.model_dump()))
        if isinstance(obj, Mapping):
            return {k: self.redact(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return type(obj)(self.redact(v) for v in obj)
        return obj

    # --- layer 3 --------------------------------------------------------------------------------

    def redact_observation(self, obs: Observation,
                           sensitive_labels: list[str] | None = None) -> Observation:
        labels = SensitiveLabels(sensitive_labels) if sensitive_labels is not None else self.labels
        out = obs.model_copy(deep=True)
        for el in out.elements:
            kind = element_sensitivity(el, labels)
            if kind:
                self.register_sensitive(el.name, kind)
                el.name = token(kind)
        return self.redact(out)


def sensitive_mask_targets(obs: Observation, sensitive_labels: list[str]) -> list[TargetSpec]:
    """Transient TargetSpecs for sensitive cells so screenshots can paint over them.

    Built from the RAW observation, so they may embed the sensitive value in a RoleName. They are
    passed straight to screenshot() and must never be logged or persisted.
    """
    labels = SensitiveLabels(sensitive_labels)
    targets = []
    for el in obs.elements:
        kind = element_sensitivity(el, labels)
        if not kind:
            continue
        strategies: list = [RoleName(role=el.role, name=el.name)]
        tc = el.table_context
        if tc and tc.row_key and tc.column_header:
            strategies.append(TableCell(section_header=tc.table_caption_or_header,
                                        row_key=tc.row_key, column_header=tc.column_header))
        targets.append(TargetSpec(frame=el.frame, strategies=strategies,
                                  description=f"mask {kind} [frame {el.frame.key}]"))
    return targets


class SecretResolver:
    """Resolves `{{secret:NAME}}` from the environment at fill time only.

    The resolved value is registered with the Redactor before it is returned, and callers must
    hand it directly to the surface; only the template is ever logged.
    """

    def __init__(self, redactor: Redactor, env: Mapping[str, str] | None = None):
        self.redactor = redactor
        self.env = env if env is not None else os.environ

    @staticmethod
    def is_template(value: str) -> bool:
        return bool(SECRET_TEMPLATE.match(value))

    def resolve(self, template: str) -> str:
        m = SECRET_TEMPLATE.match(template)
        if not m:
            raise ValueError("not a {{secret:NAME}} template")
        name = m.group(1)
        if name not in self.env:
            raise SecretMissing(name)
        value = self.env[name]
        self.redactor.register_secret(value, "SECRET")
        return value
