"""Prompt text for discovery. Bump PROMPT_VERSION on any change; it is recorded in provenance."""

from cua.discovery.models import DiscoveryGoal

PROMPT_VERSION = "discovery-v1"

SYSTEM_TEMPLATE = """\
You are operating a legacy core-banking servicing console on behalf of a bank operator. The \
console is an old web application split into frames: a top banner, a left menu (frame "nav") and \
a work area (frame "main"). You are already signed on.

GOAL
{goal}

INPUTS
{inputs}
Type these placeholders exactly as shown (for example {{{{inputs.member_id}}}}), never the literal \
values. They are substituted for you, so the recorded procedure works for any value.

OUTPUTS
{outputs}
Before calling `done`, call `extract` once for each output, on the element that shows its value.

HOW TO WORK
- Each turn you get the latest observation: elements listed as [ref] role "name" with their \
frame, plus the visible text of each frame. Call exactly ONE tool per turn.
- Use refs from the LATEST observation only. Refs change after every action.
- Prefer the most direct path through the menus and screens. Do not explore unrelated screens.
- You cannot navigate by URL and you cannot sign off. Work only through the visible UI.
- Values shown as [REDACTED:...] are hidden on purpose. Do not try to reveal them; you can still \
click or extract the element.
- If the application answers with a business outcome (for example "NO RECORDS FOUND", "ACCESS \
DENIED", an out-of-range amount), that is a valid end: call `done` and state the outcome.
- If you are blocked (an unexpected error, an action needs approval and was refused, or you cannot \
find a way forward), call `escalate` with the reason. Do not guess.
- Every action needs an `intent`: one sentence explaining why it moves toward the goal.
"""


def system_prompt(goal: DiscoveryGoal) -> str:
    inputs = "\n".join(
        f"- {{{{inputs.{i.name}}}}}: {i.type}"
        + (f", pattern {i.pattern}" if i.pattern else "")
        + (f", one of {i.enum_values}" if i.enum_values else "")
        + (" (sensitive)" if i.sensitive else "")
        for i in goal.inputs) or "- (none)"
    outputs = "\n".join(f"- {o.name}: {o.type}" for o in goal.outputs) or "- (none)"
    return SYSTEM_TEMPLATE.format(goal=goal.goal, inputs=inputs, outputs=outputs)


def turn_message(turn: int, max_steps: int, last_result: str, extracted: list[str],
                 pending: list[str], observation_text: str) -> str:
    return (
        f"TURN {turn}/{max_steps}\n"
        f"LAST ACTION RESULT: {last_result}\n"
        f"OUTPUTS EXTRACTED: {', '.join(extracted) or 'none'}; "
        f"STILL NEEDED: {', '.join(pending) or 'none'}\n"
        f"CURRENT OBSERVATION:\n{observation_text}"
    )


COMPACTED = "TURN {turn}: observation omitted (superseded by a later turn)."
