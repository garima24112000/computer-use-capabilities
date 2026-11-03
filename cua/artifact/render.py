"""Capability renderings: the tool definition (for agents) and review markdown (for humans)."""

from typing import TYPE_CHECKING, Any

from cua.artifact.conditions import Checkpoint, describe_condition
from cua.artifact.steps import Extract, Fill, Navigate, Press, Select, Step

if TYPE_CHECKING:
    from cua.artifact.profile import AppProfile
    from cua.artifact.schema import Capability


def tool_definition(cap: "Capability", profile: "AppProfile | None" = None) -> dict[str, Any]:
    """Inherited outcomes are listed by code when the profile is given, else as profile:<id>."""
    from cua.artifact.schema import tool_name

    inherited = [profile.outcome(i) if profile else None for i in cap.use_profile_outcomes]

    properties = {p.name: p.json_schema() for p in cap.inputs}
    outputs = {o.name: o.json_schema() for o in cap.outputs}
    return {
        "name": tool_name(cap.id),
        "description": f"{cap.title}. {cap.description}",
        "capability": {"id": cap.id, "version": cap.version, "status": cap.status},
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": [p.name for p in cap.inputs if p.required],
            "additionalProperties": False,
        },
        "output_schema": {
            "type": "object",
            "properties": outputs,
            "required": list(outputs),
            "additionalProperties": False,
        },
        "outcomes": [o.code for o in cap.outcomes] + [
            o.code if o else f"profile:{i}" for i, o in zip(cap.use_profile_outcomes, inherited,
                                                           strict=True)],
    }


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _checkpoint(cp: Checkpoint, indent: str = "") -> list[str]:
    lines = [f"{indent}- {cp.description} (timeout {cp.timeout_ms} ms)"]
    lines += [f"{indent}  - all of: {describe_condition(c)}" for c in cp.all_of]
    lines += [f"{indent}  - any of: {describe_condition(c)}" for c in cp.any_of or []]
    return lines


def _action(step: Step) -> str:
    a = step.action
    if isinstance(a, Navigate):
        return f"navigate `{a.url_path}`"
    if isinstance(a, Fill):
        return f"fill `{a.value}` into {a.target.description}"
    if isinstance(a, Select):
        return f"select `{a.option}` in {a.target.description}"
    if isinstance(a, Press):
        return f"press `{a.key}`" + (f" on {a.target.description}" if a.target else "")
    if isinstance(a, Extract):
        return f"extract {a.target.description} → output `{a.output}`"
    return f"click {a.target.description}"


def _target_line(step: Step) -> str | None:
    t = getattr(step.action, "target", None)
    if t is None:
        return None
    kinds = " → ".join(_strategy(s) for s in t.strategies)
    param = f"; keyed by `{{{{inputs.{', '.join(t.input_names)}}}}}`" if t.input_names else ""
    return f"frame `{t.frame.key}`; locators: {kinds}{param}"


def _strategy(s: Any) -> str:
    """Kind, plus the keys a reviewer needs to judge stability (row key, label, text)."""
    if s.kind == "table_cell":
        return f"table_cell(row={s.row_key!r}, col={s.column_header!r})"
    if s.kind == "label_anchor":
        return f"label_anchor({s.label!r})"
    if s.kind == "text_match" and not s.exact:
        return f"text_match(contains {s.text!r})"
    return s.kind


def summary_markdown(cap: "Capability") -> str:
    out = [f"# {cap.title}", "",
           f"`{cap.id}` v{cap.version} · status **{cap.status}** · "
           f"app {cap.app.product} `{cap.app.product_versions}` "
           f"(profile `{cap.app.profile}`, {cap.app.surface})",
           f"content hash `{cap.content_hash()}`", "", cap.description, ""]

    out += ["## Inputs", ""]
    if cap.inputs:
        out += ["| name | type | required | constraints | sensitive | description |",
                "|---|---|---|---|---|---|"]
        for p in cap.inputs:
            cons = []
            if p.pattern:
                cons.append(f"pattern `{p.pattern}`")
            if p.enum_values:
                cons.append("one of " + ", ".join(f"`{v}`" for v in p.enum_values))
            if p.min is not None:
                cons.append(f"≥ {p.min}")
            if p.max is not None:
                cons.append(f"≤ {p.max}")
            out.append(f"| `{p.name}` | {p.type} | {'yes' if p.required else 'no'} | "
                       f"{_cell(', '.join(cons) or '-')} | {'yes' if p.sensitive else 'no'} | "
                       f"{_cell(p.description)} |")
    else:
        out.append("_none_")
    out += ["", "## Outputs", ""]
    if cap.outputs:
        out += ["| name | type | normalize | sensitive | description |", "|---|---|---|---|---|"]
        for o in cap.outputs:
            norm = o.normalize + (f" (pattern `{o.extract_pattern}`)" if o.extract_pattern else "")
            out.append(f"| `{o.name}` | {o.type}{'?' if o.nullable else ''} | {_cell(norm)} | "
                       f"{'yes' if o.sensitive else 'no'} | {_cell(o.description)} |")
    else:
        out.append("_none_")
    if cap.secrets:
        out += ["", "## Secrets", ""]
        out += [f"- `{s.name}`: {s.purpose}" for s in cap.secrets]

    out += ["", "## Preconditions", "",
            f"- entry `{cap.preconditions.entry_path}`; session: "
            + ("profile login subflow runs first" if cap.preconditions.session == "login_subflow"
               else "none")]

    out += ["", "## Steps", ""]
    for n, step in enumerate(cap.steps, 1):
        gate = ""
        if step.requires_approval:
            gate = " · **requires approval** (" + ("approved" if step.approved
                                                   else "NOT approved") + ")"
        out.append(f"{n}. **`{step.id}`**: {step.intent}")
        out.append(f"   - action: {_action(step)}")
        out.append(f"   - risk: **{step.risk}**{gate}")
        if (line := _target_line(step)) is not None:
            out.append(f"   - {line}")
        if step.expect is not None:
            out.append("   - expect:")
            out += _checkpoint(step.expect, "     ")
        if step.retry.max_attempts > 1:
            out.append(f"   - retry: {step.retry.max_attempts}x on {', '.join(step.retry.on)}")
        if step.notes:
            out.append(f"   - notes: {step.notes}")

    out += ["", "## Business outcomes", ""]
    if cap.outcomes:
        out += ["| id | code | checked after | detect | message |", "|---|---|---|---|---|"]
        for o in cap.outcomes:
            after = "every step" if o.check_after_steps == "*" else ", ".join(
                f"`{s}`" for s in o.check_after_steps)
            detect = "; ".join(describe_condition(c) for _, c in o.detect.conditions())
            out.append(f"| `{o.id}` | `{o.code}` | {after} | {_cell(detect)} | "
                       f"{_cell(o.message_template)} |")
    if cap.use_profile_outcomes:
        inherited = ", ".join(f"`{i}`" for i in cap.use_profile_outcomes)
        out += ([""] if cap.outcomes else []) + [
            f"Inherited from profile `{cap.app.profile}`: {inherited}"]
    if not cap.outcomes and not cap.use_profile_outcomes:
        out.append("_none_")

    out += ["", "## Success", ""] + _checkpoint(cap.success)

    p = cap.provenance
    out += ["", "## Provenance", "",
            f"- source: {p.source}; recorder {p.recorder_version}"
            + (f"; model `{p.model_id}`" if p.model_id else ""),
            f"- goal (redacted): {p.goal_redacted}"]
    if p.recorded_from_run_id:
        out.append(f"- run `{p.recorded_from_run_id}` at {p.recorded_at}; evidence "
                   f"`{p.evidence_dir}`; transcript sha256 `{p.transcript_sha256}`")
    if p.git_commit:
        out.append(f"- git commit `{p.git_commit}`")
    r = cap.review
    out.append("- review: " + (f"approved by {r.reviewed_by} at {r.approved_at}"
                               + (f": {r.notes}" if r.notes else "")
                               if r.approved_at else "not reviewed"))
    return "\n".join(out) + "\n"
