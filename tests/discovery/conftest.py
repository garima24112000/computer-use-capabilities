"""Discovery test helpers: the real app + headless Chromium + ScriptedLLMClient (no network)."""

from types import SimpleNamespace

import pytest

from cua.artifact import store
from cua.discovery.llm import ScriptedLLMClient
from cua.discovery.llm import ToolCall as T
from cua.discovery.models import DiscoveryConfig, DiscoveryGoal
from cua.discovery.session import run_discovery
from cua.policy.guard import AutoApproveForTests

MEMBER_INPUT = {"name": "member_id", "value": "12345", "type": "string", "pattern": r"^\d{5}$"}
BALANCE_OUTPUT = {"name": "savings_balance", "type": "money", "normalize": "money_to_decimal"}


def click(sel: str, intent: str = "Move toward the goal.") -> T:
    return T(name="click", args={"ref": sel, "intent": intent})


def fill(sel: str, value: str, intent: str = "Enter the value.") -> T:
    return T(name="fill", args={"ref": sel, "value": value, "intent": intent})


def select(sel: str, option: str) -> T:
    return T(name="select", args={"ref": sel, "option": option, "intent": "Pick the option."})


def extract(sel: str, output: str) -> T:
    return T(name="extract", args={"ref": sel, "output": output, "intent": "Read the value."})


def done(summary: str = "Finished.") -> T:
    return T(name="done", args={"summary": summary})


def read_balance_script(member_value: str = "{{inputs.member_id}}") -> list:
    return [
        click("@button:MEMBER INQUIRY", "Open the member inquiry screen."),
        fill("@textbox:MEMBER NO.", member_value, "Enter the member number."),
        click("@link:INQUIRE", "Run the inquiry."),
        click("@link[col=NAME]", "Open the member's detail page."),
        extract("@cell[row=00,col=BALANCE]", "savings_balance"),
        done("Read the Share Savings balance."),
    ]


@pytest.fixture(autouse=True)
def _operator_secrets(monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_USER", "demo_operator")
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")


@pytest.fixture
def discover(tmp_path):
    """discover(base_url, script, member="12345", approver=..., **config) -> SimpleNamespace"""

    def run(base_url: str, script, *, member: str = "12345", inputs=None, outputs=None,
            capability_id: str = "meridian.member.read_savings_balance", approver=None,
            goal: str | None = None, description: str | None = None, **config):
        llm = script if isinstance(script, ScriptedLLMClient) else ScriptedLLMClient(script)
        g = DiscoveryGoal(
            goal=goal or f"Look up member {member} and read their Share Savings balance",
            capability_id=capability_id, title="Read a member's share savings balance",
            description=description,
            inputs=inputs if inputs is not None else [{**MEMBER_INPUT, "value": member}],
            outputs=outputs if outputs is not None else [BALANCE_OUTPUT],
            base_url=base_url)
        result = run_discovery(g, llm, approver or AutoApproveForTests(),
                               config=DiscoveryConfig(**config), runs_root=tmp_path / "runs",
                               capabilities_root=tmp_path / "capabilities")
        cap = store.load(result.artifact_path)[0] if result.artifact_path else None
        return SimpleNamespace(result=result, llm=llm, cap=cap, tmp=tmp_path,
                               run_dir=tmp_path / "runs" / result.run_id)

    return run
