"""The capability's contract: what a caller passes in, gets back, and which results are expected.

The contract is the part a calling agent depends on. Any change to it is a MAJOR version bump
(see Capability). Business outcomes are part of the contract too. "Member not found" is a
legitimate, typed result of a read, not an error.
"""

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cua.artifact.conditions import Checkpoint, InputTemplate
from cua.templating import INPUT_NAME, SECRET_NAME

SNAKE = rf"^{INPUT_NAME}$"
InputType = Literal["string", "integer", "decimal", "enum", "date"]
OutputType = Literal["string", "integer", "decimal", "money", "date"]
Normalize = Literal["none", "money_to_decimal", "strip", "digits_only"]
InputErrorCode = Literal["missing", "unknown_input", "wrong_type", "pattern_mismatch",
                         "not_in_enum", "below_min", "above_max", "invalid_date"]


def _compiles(pattern: str | None) -> str | None:
    if pattern is not None:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
    return pattern


class InputError(BaseModel):
    """A typed input validation failure. Messages never echo the values of sensitive inputs."""

    input: str
    code: InputErrorCode
    message: str


class InputParam(BaseModel):
    """One declared input. Steps reference it as `{{inputs.<name>}}`.

    `sensitive` inputs are never logged and get no `example`. In a tool definition they are
    marked writeOnly.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=SNAKE)
    type: InputType
    description: str
    required: bool = True
    pattern: str | None = None  # full-match regex, applied to the string form
    enum_values: list[str] | None = None
    min: Decimal | None = None
    max: Decimal | None = None
    sensitive: bool = False
    example: str | None = None

    @field_validator("pattern")
    @classmethod
    def _check_pattern(cls, v: str | None) -> str | None:
        return _compiles(v)

    @model_validator(mode="after")
    def _consistent(self) -> "InputParam":
        if self.sensitive and self.example is not None:
            raise ValueError(f"input {self.name!r} is sensitive and must not carry an example")
        if (self.type == "enum") != bool(self.enum_values):
            raise ValueError(f"input {self.name!r}: enum_values is required for (and only for) "
                             "type=enum")
        if (self.min is not None or self.max is not None) and self.type not in ("integer",
                                                                                "decimal"):
            raise ValueError(f"input {self.name!r}: min/max only apply to integer/decimal")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"input {self.name!r}: min > max")
        if self.example is not None and (errs := self.validate_value(self.example)):
            raise ValueError(f"input {self.name!r}: example is invalid: {errs[0].message}")
        return self

    def _err(self, code: InputErrorCode, detail: str, value: Any) -> InputError:
        shown = "<sensitive>" if self.sensitive else repr(value)
        return InputError(input=self.name, code=code, message=f"{self.name}={shown}: {detail}")

    def validate_value(self, value: Any) -> list[InputError]:
        """Errors for `value` (empty list = valid). None means "not provided"."""
        if value is None:
            return [self._err("missing", "required input missing", value)] if self.required \
                else []
        if self.type == "integer":
            if isinstance(value, bool) or not (
                    isinstance(value, int) or (isinstance(value, str)
                                               and re.fullmatch(r"-?\d+", value))):
                return [self._err("wrong_type", "expected an integer", value)]
            return self._range(Decimal(int(value)), value)
        if self.type == "decimal":
            try:
                if isinstance(value, bool):
                    raise InvalidOperation
                number = Decimal(str(value).strip())
            except InvalidOperation:
                return [self._err("wrong_type", "expected a decimal number", value)]
            if not number.is_finite():
                return [self._err("wrong_type", "expected a finite decimal number", value)]
            return self._range(number, value)
        if not isinstance(value, str):
            return [self._err("wrong_type", f"expected a string ({self.type})", value)]
        if self.type == "enum" and value not in (self.enum_values or []):
            return [self._err("not_in_enum", f"expected one of {self.enum_values}", value)]
        if self.type == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                return [self._err("invalid_date", "expected an ISO date YYYY-MM-DD", value)]
        if self.pattern is not None and not re.fullmatch(self.pattern, value):
            return [self._err("pattern_mismatch", f"does not match {self.pattern}", value)]
        return []

    def _range(self, number: Decimal, value: Any) -> list[InputError]:
        if self.min is not None and number < self.min:
            return [self._err("below_min", f"must be >= {self.min}", value)]
        if self.max is not None and number > self.max:
            return [self._err("above_max", f"must be <= {self.max}", value)]
        return []

    def json_schema(self) -> dict[str, Any]:
        s: dict[str, Any] = {"description": self.description}
        if self.type == "integer":
            s["type"] = "integer"
        elif self.type == "decimal":
            s["type"] = "number"
        else:
            s["type"] = "string"
        if self.type == "enum":
            s["enum"] = list(self.enum_values or [])
        if self.type == "date":
            s["format"] = "date"
        if self.pattern is not None:
            s["pattern"] = self.pattern
        if self.min is not None:
            s["minimum"] = float(self.min) if self.type == "decimal" else int(self.min)
        if self.max is not None:
            s["maximum"] = float(self.max) if self.type == "decimal" else int(self.max)
        if self.sensitive:
            s["writeOnly"] = True
        elif self.example is not None:
            s["examples"] = [self.example]
        return s


class OutputField(BaseModel):
    """One value the capability returns, produced by exactly one Extract step.

    The raw text read from the UI first goes through `extract_pattern`, a regex with one
    capture group that is optional, then through `normalize`. Money is returned as a decimal
    string ("12345.67") so no precision is lost.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=SNAKE)
    type: OutputType
    description: str
    sensitive: bool = False
    normalize: Normalize = "none"
    extract_pattern: str | None = None
    nullable: bool = False

    @field_validator("extract_pattern")
    @classmethod
    def _one_group(cls, v: str | None) -> str | None:
        if _compiles(v) is not None and re.compile(v).groups != 1:
            raise ValueError("extract_pattern must have exactly one capture group")
        return v

    def normalize_value(self, raw: str | None) -> str | None:
        """Apply extract_pattern and normalize. Raises ValueError if the text does not fit."""
        if raw is None or raw.strip() == "":
            if self.nullable:
                return None
            raise ValueError(f"output {self.name!r}: empty value")
        text = " ".join(raw.split())
        if self.extract_pattern is not None:
            m = re.search(self.extract_pattern, text)
            if m is None:
                raise ValueError(f"output {self.name!r}: text does not match extract_pattern")
            text = m.group(1)
        if self.normalize == "strip":
            text = text.strip()
        elif self.normalize == "digits_only":
            text = re.sub(r"\D", "", text)
        elif self.normalize == "money_to_decimal":
            text = money_to_decimal(text)
        return text

    def json_schema(self) -> dict[str, Any]:
        types = {"integer": "integer"}
        s: dict[str, Any] = {"type": types.get(self.type, "string"),
                             "description": self.description}
        if self.type in ("money", "decimal"):
            s["pattern"] = r"^-?\d+(\.\d+)?$"
        if self.type == "date":
            s["format"] = "date"
        if self.nullable:
            s["type"] = [s["type"], "null"]
        return s


def money_to_decimal(text: str) -> str:
    """"$12,345.67" -> "12345.67"; "-$1.00" and "($1.00)" -> "-1.00"."""
    t = text.strip()
    negative = t.startswith("-") or (t.startswith("(") and t.endswith(")"))
    t = re.sub(r"[\s$,()+-]", "", t)
    try:
        value = Decimal(t)
    except InvalidOperation:
        raise ValueError(f"not a money amount: {text!r}") from None
    return str(-value if negative else value)


class SecretRef(BaseModel):
    """A secret the capability needs, by environment name only. The value is never stored."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=rf"^{SECRET_NAME}$")
    purpose: str


class OutcomeSpec(BaseModel):
    """A legitimate business result that ends the run early. It is not an error.

    After each step listed in `check_after_steps` ("*" = after every step), replay evaluates
    `detect` before the step's own `expect`. On a match the run ends with
    {outcome: code, message: rendered message_template}. Outcomes let a calling agent branch on
    typed results such as MEMBER_NOT_FOUND instead of parsing error strings.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=SNAKE)
    kind: Literal["business"] = "business"
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    description: str
    message_template: InputTemplate
    detect: Checkpoint
    check_after_steps: list[str] | Literal["*"] = "*"
    # URL-path globs where this outcome can occur (empty = anywhere). Discovery inherits only
    # profile outcomes whose scope matches a page the run visited; replay only checks an
    # outcome while a frame shows an in-scope path.
    scope_paths: list[str] = Field(default_factory=list)

    def in_scope(self, paths: "set[str] | list[str]") -> bool:
        from fnmatch import fnmatch

        return not self.scope_paths or any(fnmatch(p, g) for p in paths
                                           for g in self.scope_paths)
