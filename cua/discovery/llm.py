"""The pluggable LLM seam. Discovery talks only to `LLMClient`.

Implementations:
- BedrockConverseClient: AWS Bedrock Converse API with tool use. This is the ONLY module in the
  project that imports boto3. Credentials come from the AWS profile, never from .env.
- ScriptedLLMClient: replays canned tool calls (tests and offline demos). It records everything
  it was sent, so tests can assert on prompts.

Replay never imports this module.
"""

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


# --- message model --------------------------------------------------------------------------------


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    args: dict[str, Any]


class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: str
    is_error: bool = False


Block = TextBlock | ToolUseBlock | ToolResultBlock


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: list[Block]

    def text(self) -> str:
        return "\n".join(b.text for b in self.content if isinstance(b, TextBlock))


class ToolSpec(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ToolCall(BaseModel):
    id: str = Field(default_factory=lambda: f"call_{uuid.uuid4().hex[:12]}")
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0


class LLMResponse(BaseModel):
    tool_calls: list[ToolCall] = Field(default_factory=list)
    text: str | None = None
    usage: Usage = Field(default_factory=Usage)
    raw_stop_reason: str | None = None


class LLMClient(Protocol):
    model_id: str

    def decide(self, system: str, messages: list[Message], tools: list[ToolSpec]
               ) -> LLMResponse: ...


# --- Bedrock ------------------------------------------------------------------------------------


RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException",
             "InternalServerException"}


def to_converse(messages: list[Message]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        content: list[dict[str, Any]] = []
        for b in m.content:
            if isinstance(b, TextBlock):
                content.append({"text": b.text})
            elif isinstance(b, ToolUseBlock):
                content.append({"toolUse": {"toolUseId": b.id, "name": b.name, "input": b.args}})
            else:
                content.append({"toolResult": {
                    "toolUseId": b.tool_use_id, "content": [{"text": b.content}],
                    "status": "error" if b.is_error else "success"}})
        out.append({"role": m.role, "content": content})
    return out


def from_converse(response: dict[str, Any]) -> LLMResponse:
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    calls = [ToolCall(id=b["toolUse"]["toolUseId"], name=b["toolUse"]["name"],
                      args=b["toolUse"].get("input") or {}) for b in blocks if "toolUse" in b]
    text = "\n".join(b["text"] for b in blocks if "text" in b) or None
    usage = response.get("usage", {})
    return LLMResponse(tool_calls=calls, text=text,
                       usage=Usage(input_tokens=usage.get("inputTokens", 0),
                                   output_tokens=usage.get("outputTokens", 0)),
                       raw_stop_reason=response.get("stopReason"))


class BedrockConverseClient:
    """Bedrock Converse with forced tool use (toolChoice any), temperature 0."""

    def __init__(self, model_id: str, *, profile: str | None = None, region: str | None = None,
                 max_tokens: int = 1024, max_retries: int = 4, base_backoff_s: float = 1.0,
                 client: Any = None, sleep: Callable[[float], None] = time.sleep):
        self.model_id = model_id
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.base_backoff_s = base_backoff_s
        self._sleep = sleep
        if client is None:
            import boto3  # the only boto3 import in the project

            session = boto3.Session(profile_name=profile, region_name=region)
            client = session.client("bedrock-runtime")
        self._client = client

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "BedrockConverseClient":
        import os

        env = dict(os.environ) if env is None else env
        model_id = env.get("BEDROCK_MODEL_ID", "")
        if not model_id or model_id.startswith("<"):
            raise ValueError("BEDROCK_MODEL_ID is not set (see .env.example)")
        return cls(model_id, profile=env.get("AWS_PROFILE"), region=env.get("AWS_REGION"))

    def decide(self, system: str, messages: list[Message], tools: list[ToolSpec]
               ) -> LLMResponse:
        request = {
            "modelId": self.model_id,
            "system": [{"text": system}],
            "messages": to_converse(messages),
            "inferenceConfig": {"temperature": 0, "maxTokens": self.max_tokens},
            "toolConfig": {
                "tools": [{"toolSpec": {"name": t.name, "description": t.description,
                                        "inputSchema": {"json": t.input_schema}}}
                          for t in tools],
                "toolChoice": {"any": {}},
            },
        }
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.converse(**request)
                break
            except Exception as exc:  # botocore ClientError, without importing botocore here
                code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
                if code not in RETRYABLE or attempt == self.max_retries:
                    raise
                delay = self.base_backoff_s * (2 ** attempt)
                log.warning("bedrock %s; retry %d/%d in %.1fs", code, attempt + 1,
                            self.max_retries, delay)
                self._sleep(delay)
        result = from_converse(response)
        # Never log request bodies (they contain page text); usage + stop reason only.
        log.info("bedrock usage in=%d out=%d stop=%s", result.usage.input_tokens,
                 result.usage.output_tokens, result.raw_stop_reason)
        return result


# --- scripted -----------------------------------------------------------------------------------

_ELEMENT_LINE = re.compile(r'^\[(e\d+)\] (\w+) "((?:[^"\\]|\\.)*)"(.*)$')
_ATTR = re.compile(r'(\w+)="([^"]*)"|\(label: ([^)]*)\)')
_SELECTOR = re.compile(r"^@(\w+)(?::([^\[]*))?(?:\[(.*)\])?$")


def find_ref(observation_text: str, selector: str) -> str | None:
    """Resolve "@role:name[label=..,row=..,col=..,section=..]" against prompt text.

    Unique match only. This is offline-tooling glue for scripted runs, not used by discovery.
    """
    m = _SELECTOR.match(selector)
    if m is None:
        raise ValueError(f"bad ref selector {selector!r}")
    role, name, attrs = m.group(1), m.group(2), m.group(3)
    wanted = dict(kv.split("=", 1) for kv in attrs.split(",")) if attrs else {}
    hits = []
    for line in observation_text.splitlines():
        lm = _ELEMENT_LINE.match(line)
        if not lm or lm.group(2) != role or (name is not None and lm.group(3) != name):
            continue
        found: dict[str, str] = {}
        for am in _ATTR.finditer(lm.group(4)):
            if am.group(3) is not None:
                found["label"] = am.group(3)
            else:
                found[am.group(1)] = am.group(2)
        if all(found.get(k) == v for k, v in wanted.items()):
            hits.append(lm.group(1))
    return hits[0] if len(hits) == 1 else None


ScriptEntry = ToolCall | list[ToolCall] | Callable[[list[Message]], "ToolCall | list[ToolCall]"]


class ScriptedLLMClient:
    """Plays a fixed script, one entry per decide() call.

    An entry is a ToolCall, a list of ToolCalls, or a callable(messages) returning either. A
    `ref` argument starting with "@" is resolved against the latest observation (see find_ref).
    When the script runs out, the client escalates. `calls` records (system, messages, tools)
    for every decide().
    """

    def __init__(self, script: list[ScriptEntry] | Callable[[list[Message]], Any],
                 model_id: str = "scripted"):
        self.script = script
        self.model_id = model_id
        self.calls: list[tuple[str, list[Message], list[ToolSpec]]] = []

    @classmethod
    def from_file(cls, path: str | Path) -> "ScriptedLLMClient":
        """JSON: [[{"name": "click", "args": {"ref": "@button:MEMBER INQUIRY", ...}}], ...]"""
        turns = json.loads(Path(path).read_text())
        return cls([[ToolCall(**c) for c in turn] for turn in turns],
                   model_id=f"scripted:{Path(path).name}")

    def decide(self, system: str, messages: list[Message], tools: list[ToolSpec]
               ) -> LLMResponse:
        self.calls.append((system, [m.model_copy(deep=True) for m in messages], list(tools)))
        n = len(self.calls) - 1
        if callable(self.script):
            entry = self.script(messages)
        elif n < len(self.script):
            entry = self.script[n]
        else:
            entry = ToolCall(name="escalate", args={"reason": "script exhausted"})
        if callable(entry) and not isinstance(entry, ToolCall):
            entry = entry(messages)
        calls = entry if isinstance(entry, list) else [entry]
        latest = messages[-1].text() if messages else ""
        resolved = []
        for c in calls:
            args = dict(c.args)
            ref = args.get("ref")
            if isinstance(ref, str) and ref.startswith("@"):
                args["ref"] = find_ref(latest, ref) or f"unresolved:{ref}"
            resolved.append(ToolCall(id=c.id, name=c.name, args=args))
        return LLMResponse(tool_calls=resolved, usage=Usage(input_tokens=len(latest) // 4,
                                                            output_tokens=20),
                           raw_stop_reason="tool_use")
