"""Pure tests: tool validation, Converse mapping/retries, scripted client, prompts, imports."""

import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cua.discovery.llm import (
    BedrockConverseClient,
    Message,
    ScriptedLLMClient,
    TextBlock,
    ToolCall,
    ToolResultBlock,
    ToolUseBlock,
    find_ref,
    from_converse,
    to_converse,
)
from cua.discovery.models import DiscoveryGoal
from cua.discovery.prompts import PROMPT_VERSION, system_prompt
from cua.discovery.tools import TOOLS, ToolValidationError, validate_call
from cua.surface.models import BBox, ElementInfo, FrameRef, Observation

ROOT = Path(__file__).resolve().parents[2]
MAIN = FrameRef(name_path=["main"])


def el(ref, role, name, **kw) -> ElementInfo:
    return ElementInfo(ref=ref, frame=MAIN, kind="interactive", role=role, name=name, tag="x",
                       bbox=BBox(x=0, y=0, width=1, height=1), **kw)


OBS = Observation(frames=[], text_by_frame={}, taken_at=datetime.now(UTC), elements=[
    el("e1", "textbox", "MEMBER NO."), el("e2", "link", "INQUIRE"),
    el("e3", "combobox", "ACCOUNT TYPE", options=["Share Savings", "Money Market"])])


def call(name, **args) -> ToolCall:
    return ToolCall(name=name, args=args)


def check(c: ToolCall):
    return validate_call(c, OBS, {"member_id"}, {"savings_balance"})


def test_every_tool_requires_intent_and_has_closed_schema():
    for t in TOOLS:
        assert t.input_schema["additionalProperties"] is False
        if t.name not in ("done", "escalate"):
            assert "intent" in t.input_schema["required"]


@pytest.mark.parametrize("bad, msg", [
    (call("click", ref="e9", intent="go there"), "not in the latest observation"),
    (call("click", ref="e2"), "missing required"),
    (call("click", ref="e2", intent="x y z", url="/x"), "unexpected arguments"),
    (call("fill", ref="e1", value="{{inputs.other}}", intent="type it"), "not a declared input"),
    (call("fill", ref="e1", value="{{secret:BANK_OPERATOR_PASS}}", intent="type"),
     "secrets are not available"),
    (call("fill", ref="e1", value="{{ 1+1 }}", intent="type it"), "unsupported placeholder"),
    (call("press", key="F5", intent="reload page"), "not allowed"),
    (call("extract", ref="e2", output="balance", intent="read it"), "not declared"),
    (call("select", ref="e3", option="Gold", intent="pick it"), "not in"),
    (call("navigate", url="/x"), "unknown tool"),
])
def test_invalid_calls_are_rejected_before_execution(bad, msg):
    with pytest.raises(ToolValidationError, match=msg):
        check(bad)


def test_valid_calls():
    v = check(call("fill", ref="e1", value="{{inputs.member_id}}", intent="type the number"))
    assert (v.name, v.ref, v.value, v.is_action) == ("fill", "e1", "{{inputs.member_id}}", True)
    assert check(call("press", key="Enter", intent="submit it")).key == "Enter"
    assert check(call("done", summary="all done")).text == "all done"


def test_converse_mapping_round_trip():
    msgs = [Message(role="user", content=[TextBlock(text="hi")]),
            Message(role="assistant", content=[ToolUseBlock(id="t1", name="click",
                                                            args={"ref": "e1"})]),
            Message(role="user", content=[ToolResultBlock(tool_use_id="t1", content="bad",
                                                          is_error=True)])]
    out = to_converse(msgs)
    assert out[1]["content"][0] == {"toolUse": {"toolUseId": "t1", "name": "click",
                                                "input": {"ref": "e1"}}}
    assert out[2]["content"][0]["toolResult"]["status"] == "error"
    resp = from_converse({"output": {"message": {"content": [
        {"text": "thinking"}, {"toolUse": {"toolUseId": "u1", "name": "done",
                                           "input": {"summary": "ok"}}}]}},
        "usage": {"inputTokens": 10, "outputTokens": 3}, "stopReason": "tool_use"})
    assert resp.tool_calls[0].name == "done" and resp.usage.input_tokens == 10
    assert resp.text == "thinking" and resp.raw_stop_reason == "tool_use"


class _Throttled(Exception):
    response = {"Error": {"Code": "ThrottlingException"}}


class FakeBedrock:
    def __init__(self, failures: int):
        self.failures = failures
        self.requests: list[dict] = []

    def converse(self, **request):
        self.requests.append(request)
        if self.failures:
            self.failures -= 1
            raise _Throttled()
        return {"output": {"message": {"content": [{"toolUse": {
            "toolUseId": "x", "name": "escalate", "input": {"reason": "r"}}}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1}, "stopReason": "tool_use"}


def test_bedrock_request_shape_and_bounded_retries():
    sleeps: list[float] = []
    fake = FakeBedrock(failures=2)
    client = BedrockConverseClient("model-x", client=fake, sleep=sleeps.append)
    resp = client.decide("sys", [Message(role="user", content=[TextBlock(text="go")])], TOOLS)
    assert resp.tool_calls[0].name == "escalate" and sleeps == [1.0, 2.0]
    req = fake.requests[0]
    assert req["toolConfig"]["toolChoice"] == {"any": {}}
    assert req["inferenceConfig"] == {"temperature": 0, "maxTokens": 1024}
    assert req["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]["type"] == "object"
    with pytest.raises(_Throttled):
        BedrockConverseClient("m", client=FakeBedrock(failures=9), sleep=lambda s: None
                              ).decide("s", [], TOOLS)


def test_bedrock_from_env_requires_model_id():
    with pytest.raises(ValueError, match="BEDROCK_MODEL_ID"):
        BedrockConverseClient.from_env({"BEDROCK_MODEL_ID": "<inference profile id>"})


def test_scripted_client_resolves_selectors_and_records_calls():
    text = ('[e7] button "MEMBER INQUIRY" frame=nav\n'
            '[e9] textbox "MEMBER NO." (label: MEMBER NO.) frame=main\n'
            '[e30] cell "$1.00" section="ACCOUNTS" row="00" col="BALANCE" frame=main\n'
            '[e33] cell "$2.00" section="ACCOUNTS" row="10" col="BALANCE" frame=main')
    assert find_ref(text, "@button:MEMBER INQUIRY") == "e7"
    assert find_ref(text, "@textbox[label=MEMBER NO.]") == "e9"
    assert find_ref(text, "@cell[row=00,col=BALANCE]") == "e30"
    assert find_ref(text, "@cell[col=BALANCE]") is None  # ambiguous
    llm = ScriptedLLMClient([ToolCall(name="click", args={"ref": "@button:MEMBER INQUIRY",
                                                          "intent": "i"})])
    msgs = [Message(role="user", content=[TextBlock(text=text)])]
    assert llm.decide("sys", msgs, TOOLS).tool_calls[0].args["ref"] == "e7"
    assert llm.decide("sys", msgs, TOOLS).tool_calls[0].name == "escalate"  # exhausted
    assert len(llm.calls) == 2 and llm.calls[0][0] == "sys"


def test_system_prompt_mentions_contract_and_rules():
    goal = DiscoveryGoal(goal="Read the balance for member 12345", capability_id="a.b",
                         title="t", base_url="http://x",
                         inputs=[{"name": "member_id", "value": "12345",
                                  "pattern": r"^\d{5}$"}],
                         outputs=[{"name": "savings_balance", "type": "money"}])
    text = system_prompt(goal)
    for needle in ("{{inputs.member_id}}", "savings_balance", "ONE tool per turn",
                   "[REDACTED:...]", "escalate", "NO RECORDS FOUND", "cannot navigate by URL"):
        assert needle in text
    assert PROMPT_VERSION == "discovery-v1"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_only_llm_module_imports_boto3_and_shared_code_never_imports_discovery():
    for path in (ROOT / "cua").rglob("*.py"):
        imports = _imports(path)
        if path.name != "llm.py":
            assert not any(i.startswith(("boto3", "botocore")) for i in imports), path
        if "discovery" not in path.parts and path.name != "cli.py":
            assert not any(i.startswith("cua.discovery") for i in imports), path
