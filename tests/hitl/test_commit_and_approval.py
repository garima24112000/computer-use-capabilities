"""commit_uncertain → Mark handled; approval_required → Approve / Deny from the console."""

from cua.artifact import store as artifact_store
from cua.discovery.llm import ScriptedLLMClient
from cua.discovery.models import DiscoveryGoal
from cua.discovery.session import run_discovery
from cua.policy.guard import DenyAllApprover
from cua.replay.harness import FaultInjection as F
from cua.replay.session import run_replay
from tests.discovery.test_generalization import OPEN_INPUTS, _open_script
from tests.hitl.conftest import OPEN, Background, accounts_of, isolated_store


def test_commit_uncertain_is_marked_handled_with_a_note(app_a, tmp_path, console):
    caps = isolated_store(tmp_path, OPEN, "1.1.0", approve=True)
    cap, _ = artifact_store.load(caps / OPEN / "1.1.0.json")
    submit = next(s.id for s in cap.steps if s.risk == "irreversible")
    inputs = {"member_id": "55555", "account_type": "Money Market", "initial_deposit": "250.00",
              "nickname": ""}
    run = Background(lambda: run_replay(
        f"{OPEN}@1.1.0", inputs, base_url=app_a, runs_root=tmp_path / "runs",
        capabilities_root=caps, hitl=True, hitl_store=console.store, headless=True,
        injections=[F(faults={"expire_session_now": True}, at_step=submit)]))
    iv = console.wait_open("commit_uncertain")
    assert iv["failure_category"] == "session_expired_after_commit" and iv["step_id"] == submit
    assert console.cmd(iv["id"], "handled", operator="frank").status_code == 400  # note needed
    note = "checked the core: no new sub-account exists; safe to retry"
    assert console.cmd(iv["id"], "handled", operator="frank", note=note).status_code == 200
    result = run.join()
    assert result.status == "escalated", result.failure  # shows any escalation_error detail
    assert result.failure.category == "session_expired_after_commit"
    assert result.failure.escalation["resolution"] == note
    assert result.irreversible_executed == [submit]
    assert console.store.control(iv["run_id"]).state == "DONE"


def _discover_open(app_a, tmp_path, console, member: str):
    goal = DiscoveryGoal(
        goal=f"Open a Money Market sub-account for member {member} with 250.00",
        capability_id=OPEN, title="Open a sub-account",
        inputs=[{**OPEN_INPUTS[0], "value": member}, *OPEN_INPUTS[1:]],
        outputs=[{"name": "confirmation_number", "type": "string", "normalize": "strip",
                  "extract_pattern": r"(MC-\d{6})"}],
        base_url=app_a)
    script = _open_script()
    script[1] = script[1].model_copy(update={"args": {**script[1].args,
                                                      "value": "{{inputs.member_id}}"}})
    return Background(lambda: run_discovery(
        goal, ScriptedLLMClient(script), DenyAllApprover(), runs_root=tmp_path / "runs",
        capabilities_root=tmp_path / "caps", console_approval=True,
        hitl_store=console.store, headless=True))


def test_console_deny_then_approve_the_submit(app_a, tmp_path, console):
    before = accounts_of(app_a, "70001").count("Money Market")
    run = _discover_open(app_a, tmp_path, console, "70001")
    iv = console.wait_open("approval_required")
    assert iv["approval"]["decision"]["risk"] == "irreversible"
    assert iv["approval"]["decision"]["rule_id"] == "subacct-submit"
    assert "SUBMIT" in iv["approval"]["action"]
    assert console.cmd(iv["id"], "claim", operator="gina").status_code == 200  # optional
    assert console.cmd(iv["id"], "deny", operator="gina").status_code == 200
    denied = run.join()
    assert denied.status == "escalated" and "ApprovalDenied" in denied.reason
    assert accounts_of(app_a, "70001").count("Money Market") == before  # nothing created

    run = _discover_open(app_a, tmp_path, console, "70001")
    iv2 = console.wait_open("approval_required", exclude={iv["id"]})
    assert console.cmd(iv2["id"], "approve", operator="gina").status_code == 200
    approved = run.join()
    assert approved.status == "completed", approved.reason
    assert approved.outputs["confirmation_number"].startswith("MC-")
    assert approved.irreversible_executed
    assert accounts_of(app_a, "70001").count("Money Market") == before + 1
    assert console.get(iv2["id"])["resolution"] == "approved"


def test_discovery_stuck_resume_tells_the_model_what_the_human_did(app_a, tmp_path, console,
                                                                    human):
    from tests.discovery.conftest import BALANCE_OUTPUT, MEMBER_INPUT, click, read_balance_script

    script = [click("@text:FUNCTIONS")] * 3 + read_balance_script()[1:]  # stuck, then fine
    llm = ScriptedLLMClient(script)
    goal = DiscoveryGoal(goal="Look up member 12345 and read their Share Savings balance",
                         capability_id="meridian.member.read_savings_balance",
                         title="Read savings balance", inputs=[MEMBER_INPUT],
                         outputs=[BALANCE_OUTPUT], base_url=app_a)
    run = Background(lambda: run_discovery(
        goal, llm, DenyAllApprover(), runs_root=tmp_path / "runs",
        capabilities_root=tmp_path / "caps", hitl=True, hitl_store=console.store,
        headless=True))
    iv = console.wait_open("discovery_stuck")
    assert "no progress" in iv["reason"] and iv["goal"].startswith("Look up member")
    console.cmd(iv["id"], "claim", operator="hank")
    console.wait_control(iv["id"], "HUMAN")
    h = human(iv["live_view"]["cdp_url"])
    h.nav.click("text=MEMBER INQUIRY")
    h.main.wait_for_selector("input[name=txtA]")
    from tests.hitl.conftest import wait_for
    wait_for(lambda: console.get(iv["id"])["human_actions"], 20, "human actions")
    console.cmd(iv["id"], "resume", operator="hank")
    result = run.join()

    assert result.status == "completed", result.reason
    assert result.outputs == {"savings_balance": "12345.67"}
    resumed_turn = next(m for _, msgs, _ in llm.calls for m in msgs[-1:]
                        if "A human operator intervened" in m.text())
    text = resumed_turn.text()
    assert 'click button "MEMBER INQUIRY"' in text and "Continue from the current page." in text
    cap = artifact_store.load(result.artifact_path)[0]
    assert cap.steps[0].id == "s01_fill_member_no"  # the human's click is evidence, not a step
