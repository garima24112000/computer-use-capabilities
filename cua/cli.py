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
