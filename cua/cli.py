"""Typer CLI entry point `cua`. Commands for discover/replay/console are added in later steps."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from cua import __version__

app = typer.Typer(help="Computer-use automation: discover, save and replay capabilities.",
                  no_args_is_help=True)
artifact_app = typer.Typer(help="Validate, inspect and approve capability artifacts.",
                           no_args_is_help=True)
profile_app = typer.Typer(help="App profiles (configs/app_profiles/*.yaml).",
                          no_args_is_help=True)
app.add_typer(artifact_app, name="artifact")
app.add_typer(profile_app, name="profile")

REVIEWER = re.compile(r"^[A-Za-z0-9_.\- ]{1,64}$")


@app.callback()
def main() -> None:
    """Computer-use automation CLI."""


@app.command()
def version() -> None:
    """Print the cua package version."""
    typer.echo(__version__)


def _load(path: Path):
    from cua.artifact import store

    try:
        return store.load(path)
    except ValidationError as exc:
        typer.echo(f"INVALID {path}\n{exc}", err=True)
        raise typer.Exit(1) from None


@artifact_app.command("validate")
def artifact_validate(path: Path) -> None:
    """Validate + lint a capability JSON (exit 1 on errors; warnings are printed)."""
    cap, report = _load(path)
    typer.echo(f"OK {cap.id} v{cap.version} ({cap.status}) {cap.content_hash()}")
    for w in report.warnings:
        typer.echo(f"WARN {w}")


@artifact_app.command("show")
def artifact_show(path: Path) -> None:
    """Print the human review rendering (markdown)."""
    cap, _ = _load(path)
    typer.echo(cap.summary_markdown(), nl=False)


@artifact_app.command("tool")
def artifact_tool(path: Path) -> None:
    """Print the tool definition a calling agent sees (JSON)."""
    from cua.artifact.profile import PROFILES_DIR, AppProfile

    cap, _ = _load(path)
    profile_path = PROFILES_DIR / f"{cap.app.profile}.yaml"
    profile = AppProfile.load(profile_path) if profile_path.exists() else None
    typer.echo(json.dumps(cap.to_tool_definition(profile), indent=2, ensure_ascii=False))


@artifact_app.command("schema")
def artifact_schema(
    out: Annotated[Path | None, typer.Option(help="output dir (default: schemas/)")] = None,
) -> None:
    """Export JSON Schemas for capabilities and app profiles."""
    from cua.artifact import store

    for path in store.export_schemas(out or store.SCHEMAS_DIR):
        typer.echo(f"wrote {path}")


@artifact_app.command("approve")
def artifact_approve(
    path: Path,
    reviewer: Annotated[str, typer.Option("--reviewer", help="who reviewed it")],
    note: Annotated[str | None, typer.Option("--note", help="review note")] = None,
) -> None:
    """Approve every gated step, set status=approved, and record the approved content hash."""
    from cua.artifact import store
    from cua.policy.redaction import Redactor

    if not REVIEWER.match(reviewer):
        typer.echo("reviewer must be a plain name/handle (letters, digits, . _ - space)", err=True)
        raise typer.Exit(2)
    cap, _ = _load(path)
    if cap.status == "deprecated":
        typer.echo(f"{cap.id} v{cap.version} is deprecated; refusing to approve", err=True)
        raise typer.Exit(1)
    gated = [s for s in cap.steps if s.requires_approval]
    steps = [s.model_copy(update={"approved": True}) if s.requires_approval else s
             for s in cap.steps]
    review = cap.review.model_copy(update={
        "reviewed_by": reviewer,
        "approved_at": datetime.now(UTC).replace(microsecond=0),
        "notes": Redactor().redact_text(note) if note else None,
        "approved_content_hash": cap.content_hash(),
    })
    approved = cap.model_copy(update={"steps": steps, "review": review, "status": "approved"})
    approved, _ = store.parse(store.canonical_json(approved))  # re-validate + lint
    store.save(approved, path=path)  # allowed: content hash unchanged
    typer.echo(f"APPROVED {approved.id} v{approved.version} by {reviewer}")
    typer.echo(f"  content hash {approved.review.approved_content_hash}")
    if gated:
        for s in gated:
            typer.echo(f"  approved step {s.id} ({s.risk}): {s.intent}")
    else:
        typer.echo("  no steps require approval")


INPUT_HELP = (
    "name=value:type[:extra] (repeatable). type: string|integer|decimal|enum|date. extra is a "
    "regex pattern, or for enum the allowed values separated by '|' (the value must be one of "
    "them). Suffix the name with '?' to make the input optional, e.g. "
    "'nickname?=Rainy Day:string:^[A-Za-z0-9 ]{0,20}$' or 'nickname?=:string' to leave it empty."
)
OUTPUT_HELP = ("name:type[:normalize] (repeatable). type: string|integer|decimal|money|date; "
               "normalize: none|strip|digits_only|money_to_decimal.")


def parse_input(spec: str) -> dict:
    """name[?]=value:type[:pattern | :enum1|enum2|...]  (a pattern may itself contain ':')."""
    name, sep, rest = spec.partition("=")
    if not sep or not name:
        raise ValueError(f"--input {spec!r}: expected name=value:type[:extra]")
    required = not name.endswith("?")
    name = name.removesuffix("?")
    value, _, rest = rest.partition(":")
    type_, _, extra = rest.partition(":")
    out = {"name": name, "value": value, "type": type_ or "string", "required": required}
    if out["type"] == "enum":
        out["enum_values"] = [v for v in extra.split("|") if v] or None
    elif extra:
        out["pattern"] = extra
    return out


def parse_output(spec: str) -> dict:
    """name:type[:normalize]"""
    name, _, rest = spec.partition(":")
    type_, _, normalize = rest.partition(":")
    return {"name": name, "type": type_ or "string", "normalize": normalize or "none"}


def _usage_error(message: str) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(2)


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="what the operator wants done")],
    capability_id: Annotated[str, typer.Option("--id", help="capability id, e.g. a.b.c")],
    title: Annotated[str | None, typer.Option(help="capability title")] = None,
    inputs: Annotated[list[str] | None, typer.Option("--input", help=INPUT_HELP)] = None,
    outputs: Annotated[list[str] | None, typer.Option("--output", help=OUTPUT_HELP)] = None,
    tenant: Annotated[str, typer.Option(help="target app tenant")] = "a",
    headed: Annotated[bool, typer.Option(help="show the browser")] = False,
    max_steps: Annotated[int, typer.Option(help="LLM turn budget")] = 25,
    start_app: Annotated[bool, typer.Option(
        help="start the target app in the background on a free port")] = True,
    base_url: Annotated[str | None, typer.Option(help="target app URL (--no-start-app)")] = None,
    approve_mode: Annotated[str, typer.Option(help="prompt | deny")] = "prompt",
    llm: Annotated[str, typer.Option(help="bedrock | scripted:<path>")] = "bedrock",
    trace: Annotated[bool, typer.Option(help="record a Playwright trace (UNMASKED DOM)")] = False,
    runs_dir: Annotated[Path, typer.Option(help="where run evidence goes")] = Path("runs"),
    capabilities_dir: Annotated[Path | None, typer.Option(
        help="where artifacts are saved (default: capabilities/)")] = None,
    debug: Annotated[bool, typer.Option(help="show full tracebacks")] = False,
) -> None:
    """Discover a capability with the LLM agent and save it as a draft artifact.

    Everything is validated before anything runs (contract, input values, LLM config, secrets):
    a bad invocation exits 2 without starting the app, the browser or the LLM.
    """
    import contextlib
    import os

    from dotenv import load_dotenv

    from cua.artifact import store
    from cua.discovery.llm import BedrockConverseClient, ScriptedLLMClient
    from cua.discovery.models import ContractError, DiscoveryConfig, DiscoveryGoal, build_contract
    from cua.discovery.session import missing_secrets, run_discovery
    from cua.hitl.terminal import TerminalApprover
    from cua.policy.guard import DenyAllApprover
    from target_app import background

    # --- 1. validate everything; no side effects yet ---------------------------------------
    load_dotenv()
    try:
        in_specs = [parse_input(i) for i in inputs or []]
        out_specs = [parse_output(o) for o in outputs or []]
        build_contract(capability_id, in_specs, out_specs)
    except ContractError as exc:
        raise _usage_error(str(exc)) from None
    except ValueError as exc:
        raise _usage_error(str(exc)) from None
    if approve_mode not in ("prompt", "deny"):
        raise _usage_error("--approve-mode must be prompt or deny")
    if not start_app and not base_url:
        raise _usage_error("--no-start-app needs --base-url")
    if missing := missing_secrets():
        raise _usage_error(f"missing operator secrets in the environment: {missing} "
                           "(see .env.example)")
    try:
        if llm.startswith("scripted:"):
            client = ScriptedLLMClient.from_file(llm.removeprefix("scripted:"))
        elif llm == "bedrock":
            client = BedrockConverseClient.from_env(dict(os.environ))
        else:
            raise ValueError("--llm must be bedrock or scripted:<path>")
    except Exception as exc:
        if debug:
            raise
        raise _usage_error(f"LLM setup failed: {type(exc).__name__}: {exc}") from None
    approver = TerminalApprover() if approve_mode == "prompt" else DenyAllApprover()

    # --- 2. run (side effects from here on) ------------------------------------------------
    try:
        app_ctx = background.serve_in_thread(tenant) if start_app \
            else contextlib.nullcontext(base_url)
        with app_ctx as url:
            g = DiscoveryGoal(goal=goal, capability_id=capability_id, title=title or goal,
                              inputs=in_specs, outputs=out_specs, tenant=tenant, base_url=url)
            result = run_discovery(g, client, approver,
                                   config=DiscoveryConfig(max_steps=max_steps),
                                   headless=not headed, trace=trace, runs_root=runs_dir,
                                   capabilities_root=capabilities_dir or store.CAPABILITIES_DIR)
    except Exception as exc:
        if debug:
            raise
        typer.echo(f"error: discovery failed: {type(exc).__name__}: {exc} "
                   "(re-run with --debug for the traceback)", err=True)
        raise typer.Exit(1) from None

    # --- 3. report -------------------------------------------------------------------------
    typer.echo(f"status    : {result.status}"
               + (f" ({result.outcome})" if result.outcome else "")
               + (f" - {result.reason}" if result.reason else ""))
    typer.echo(f"turns     : {result.turns}  actions: {result.actions_executed}  "
               f"recorded steps: {result.recorded_steps}")
    if result.irreversible_executed:
        typer.echo("IRREVERSIBLE actions executed: "
                   + "; ".join(result.irreversible_steps_executed))
    typer.echo(f"tokens    : in {result.usage.input_tokens} / out {result.usage.output_tokens}"
               f"  wall {result.wall_s}s")
    typer.echo(f"run dir   : {result.run_dir}  (summary.json, events.jsonl)")
    if result.artifact_path:
        typer.echo(f"artifact  : {result.artifact_path}  (draft; review with "
                   "`cua artifact show`, then `cua artifact approve`)")
    if result.rejected_path:
        typer.echo(f"REJECTED  : {result.rejected_path}")
        for e in result.lint_errors:
            typer.echo(f"  {e}")
    ok = result.status == "completed" and result.artifact_path is not None
    raise typer.Exit(0 if ok else 1)


@profile_app.command("validate")
def profile_validate(path: Path) -> None:
    """Validate + lint an app profile YAML."""
    from cua.artifact.profile import AppProfile

    try:
        profile = AppProfile.load(path)
    except ValidationError as exc:
        typer.echo(f"INVALID {path}\n{exc}", err=True)
        raise typer.Exit(1) from None
    report = profile.lint()
    typer.echo(f"OK {profile.id} ({len(profile.login_subflow)} login steps, "
               f"{len(profile.outcomes)} outcomes, {len(profile.sensitive_labels)} "
               "sensitive labels)")
    for w in report.warnings:
        typer.echo(f"WARN {w}")


if __name__ == "__main__":
    app()
