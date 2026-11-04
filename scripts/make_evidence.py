"""Regenerate evidence/replay/ (reproducible replay evidence).

Starts the target app (tenant A, and tenant B for the drift scenarios) on free ports, runs each
scenario through the REAL `cua replay` CLI in a subprocess (so console.txt is the exact output),
and writes evidence/replay/<scenario>/:

    command.txt   the command line
    console.txt   exact stdout+stderr, then the exit code
    result.json   the ReplayResult (a list of them for --repeat)
    events.jsonl  the run's event log
    screenshots/, snapshots/   only for failure / recovery scenarios

Then it rewrites local absolute paths to repo-relative ones, checks every scenario's expected
exit code / status, and writes the summary table into evidence/README.md (between the
`replay-table` markers; a run of selected scenarios replaces only their rows). It also writes
evidence/catalog/tools.json (`cua catalog list --json`). Idempotent: a full run wipes and
regenerates evidence/replay/ only.

Usage: uv run python scripts/make_evidence.py [scenario ...]
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from target_app.background import free_port  # noqa: E402

EVIDENCE = ROOT / "evidence"
OUT = EVIDENCE / "replay"
WORK = Path("runs") / "_evidence"  # relative to ROOT (the subprocess cwd); gitignored
READ = "meridian.member.read_savings_balance@1.1.0"
OPEN = "meridian.subaccount.open@1.1.0"
OPEN_ID = "meridian.subaccount.open"
EXIT = {"success": 0, "business_outcome": 10, "failed": 20, "escalated": 30}
START, END = "<!-- replay-table:start -->", "<!-- replay-table:end -->"


@dataclass
class Scenario:
    name: str
    proves: str
    args: list[str]
    status: str  # expected ReplayResult.status
    exit: int
    tenant: str = "a"
    category: str | None = None  # expected failure category
    recovery: str | None = None  # expected recovery kind
    keep_images: bool = False  # failure / recovery: copy screenshots + snapshots
    store: str | None = None  # "draft_open": a temp store with a DRAFT copy of subaccount.open
    command: list[str] = field(default_factory=lambda: ["replay"])  # or ["catalog", "invoke"]


def inj(step: str, faults: dict) -> list[str]:
    return ["--inject-at", step, json.dumps(faults)]


def open_inputs(member: str, kind: str, deposit: str) -> list[str]:
    return ["--input", f"member_id={member}", "--input", f"account_type={kind}",
            "--input", f"initial_deposit={deposit}"]


SCENARIOS = [
    Scenario("success_12345", "Happy path: deterministic replay, no LLM",
             [READ, "--input", "member_id=12345"], "success", 0),
    Scenario("success_31415", "Same artifact, different input (row key is templated)",
             [READ, "--input", "member_id=31415"], "success", 0),
    Scenario("outcome_not_found_99999", "Business outcome, not a failure (member_not_found)",
             [READ, "--input", "member_id=99999"], "business_outcome", 10),
    Scenario("outcome_access_denied_40300", "Business outcome (access_denied)",
             [READ, "--input", "member_id=40300"], "business_outcome", 10),
    Scenario("input_invalid_abc", "Contract validation before any browser starts",
             [READ, "--input", "member_id=abc"], "failed", 20, category="input_validation"),
    Scenario("recovery_interstitial", "Maintenance interstitial dismissed, step re-verified",
             [READ, "--input", "member_id=12345",
              *inj("s03_click_inquire", {"interstitial_count": 1})],
             "success", 0, recovery="interstitial", keep_images=True),
    Scenario("recovery_dialog", "Unexpected JS confirm answered per the app profile",
             [READ, "--input", "member_id=12345", "--inject", '{"js_confirm_count": 1}'],
             "success", 0, recovery="dialog", keep_images=True),
    Scenario("recovery_session_relogin", "Session expiry before any commit: re-login + resume",
             [READ, "--input", "member_id=12345",
              *inj("s03_click_inquire", {"expire_session_now": True})],
             "success", 0, recovery="session_relogin", keep_images=True),
    Scenario("recovery_slow_settle",
             "Page slower than the 15 s settle budget; the step's checkpoint absorbs it",
             [READ, "--input", "member_id=12345",
              *inj("s03_click_inquire", {"latency_ms": 17000, "latency_count": 1})],
             "success", 0, recovery="settle_absorbed_by_checkpoint", keep_images=True),
    Scenario("hard_app_error_500", "Hard app error: typed failure + screenshot + snapshot",
             [READ, "--input", "member_id=12345",
              *inj("s04_click_name", {"http500_count": 1, "http500_path_prefix": "/member"})],
             "failed", 20, category="app_error", keep_images=True),
    Scenario("drift_tenant_b_override", "UI drift (tenant B) handled by the tenant override",
             [READ, "--input", "member_id=12345", "--tenant", "b"], "success", 0, tenant="b"),
    Scenario("drift_tenant_b_no_override",
             "Raw drift: a risky step is never run on a CSS-path-only match",
             [READ, "--input", "member_id=12345", "--tenant", "b", "--no-override"],
             "failed", 20, tenant="b", category="structural_only_on_risky", keep_images=True),
    Scenario("stability_repeat_5", "Determinism: 5 fresh replays, identical steps + outputs",
             [READ, "--input", "member_id=12345", "--repeat", "5"], "success", 0),
    Scenario("subaccount_unapproved_refused", "Unapproved artifact with an irreversible step "
             "is refused before any browser", [OPEN, *open_inputs("31415", "Share Savings",
                                                                  "100.00")],
             "failed", 20, category="artifact_not_approved", store="draft_open"),
    Scenario("subaccount_approved_success", "Approved irreversible capability commits once",
             [OPEN, *open_inputs("31415", "Share Savings", "100.00")], "success", 0),
    Scenario("subaccount_deposit_out_of_bounds", "Deposit 20000 rejected by the contract bounds",
             [OPEN, *open_inputs("31415", "Share Savings", "20000")], "failed", 20,
             category="input_validation"),
    Scenario("commit_uncertain_session_after_submit",
             "Session lost right after SUBMIT: never retried, never re-logged in",
             [OPEN, *open_inputs("55555", "Money Market", "250.00"),
              *inj("s07_click_submit", {"expire_session_now": True})],
             "failed", 20, category="session_expired_after_commit", keep_images=True),
    Scenario("catalog_invoke_read_balance",
             "An agent calls the capability by tool name with typed JSON args (same replay path)",
             ["meridian_member_read_savings_balance", "--args", '{"member_id": "12345"}'],
             "success", 0, command=["catalog", "invoke"]),
]


# --- target app ---------------------------------------------------------------------------------


class App:
    def __init__(self, tenant: str):
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "target_app", "--port", str(self.port), "--tenant", tenant],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{self.url}/__faults", timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"target app (tenant {tenant}) did not start")

    def reset(self) -> None:
        httpx.post(f"{self.url}/__faults/reset", timeout=5).raise_for_status()

    def stop(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=10)


# --- helpers ------------------------------------------------------------------------------------


def sanitize_text(text: str) -> str:
    """Local absolute paths -> repo-relative; anything else under $HOME -> ~."""
    text = text.replace(str(ROOT) + "/", "").replace(str(ROOT), ".")
    return text.replace(str(Path.home()), "~")


def sanitize_tree(path: Path) -> None:
    for p in path.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".txt", ".md", ".diff"):
            p.write_text(sanitize_text(p.read_text()))


def _rewrite(path: Path, old: str, new: str) -> None:
    for p in path.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".txt"):
            p.write_text(p.read_text().replace(old, new))


def draft_open_store() -> Path:
    """A store holding a DRAFT copy of subaccount.open 1.1.0 (approval stripped)."""
    root = WORK / "_draft_store"
    shutil.rmtree(ROOT / root, ignore_errors=True)
    (ROOT / root / OPEN_ID).mkdir(parents=True)
    data = json.loads((ROOT / "capabilities" / OPEN_ID / "1.1.0.json").read_text())
    data["status"], data["review"] = "draft", {}
    for step in data["steps"]:
        step["approved"] = False
    (ROOT / root / OPEN_ID / "1.1.0.json").write_text(json.dumps(data, indent=2))
    return root


def key_line(s: Scenario, results: list[dict], console: str) -> str:
    if len(results) > 1:
        lines = console.splitlines()
        i = next(i for i, x in enumerate(lines) if x.startswith("STABILITY"))
        return "; ".join(" ".join(x.split()) for x in lines[i:i + 3])
    r = results[0]
    parts = []
    if r.get("outputs"):
        parts += [f"{k} = {v}" for k, v in r["outputs"].items()]
    if r.get("outcome"):
        parts.append(f"outcome {r['outcome']['code']} ({r['outcome']['id']})")
    if r.get("failure"):
        f = r["failure"]
        parts.append(f"{f['category']} at {f['step_id'] or '-'} (retryable="
                     f"{str(f['retryable']).lower()})")
    parts += [f"recovery {x['kind']}:{x['id']} at {x['step_id']}" for x in r["recoveries"]]
    if r.get("irreversible_executed"):
        parts.append(f"irreversible executed: {', '.join(r['irreversible_executed'])}")
    if s.name.startswith("drift") and r.get("drift"):
        parts.append(f"drift at {', '.join(sorted({d['step_id'] for d in r['drift']}))}")
    return "; ".join(parts)


def check(s: Scenario, code: int, results: list[dict]) -> list[str]:
    problems = []
    if code != s.exit:
        problems.append(f"exit {code} != {s.exit}")
    for r in results:
        if r["status"] != s.status:
            problems.append(f"status {r['status']} != {s.status}")
        cat = (r.get("failure") or {}).get("category")
        if s.category and cat != s.category:
            problems.append(f"category {cat} != {s.category}")
        if s.recovery and s.recovery not in {x["kind"] for x in r["recoveries"]}:
            problems.append(f"no {s.recovery} recovery")
    if not results:
        problems.append("no result.json")
    return sorted(set(problems))


def run_scenario(s: Scenario, apps: dict[str, App]) -> dict:
    app = apps[s.tenant]
    app.reset()
    work = WORK / s.name
    shutil.rmtree(ROOT / work, ignore_errors=True)
    args = [*s.command, *s.args, "--no-start-app", "--base-url", app.url, "--runs-dir", str(work)]
    if s.store == "draft_open":
        args += ["--capabilities-dir", str(draft_open_store())]
    shown = "uv run cua " + " ".join(_quote(a) for a in args)
    proc = subprocess.run([sys.executable, "-m", "cua.cli", *args], cwd=ROOT,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                          env={**os.environ, "COLUMNS": "200"})
    console = proc.stdout + f"\n[exit code {proc.returncode}]\n"

    dst = OUT / s.name
    dst.mkdir(parents=True)
    (dst / "command.txt").write_text(shown + "\n")
    (dst / "console.txt").write_text(console)
    run_dirs = sorted(p for p in (ROOT / work).iterdir() if p.is_dir() and
                      (p / "result.json").exists()) if (ROOT / work).exists() else []
    results = [json.loads((p / "result.json").read_text()) for p in run_dirs]
    if results:
        (dst / "result.json").write_text(json.dumps(results[0] if len(results) == 1
                                                    else results, indent=2) + "\n")
        (dst / "events.jsonl").write_text("".join((p / "events.jsonl").read_text()
                                                  for p in run_dirs))
    if s.keep_images:
        for p in run_dirs:
            for sub in ("screenshots", "snapshots"):
                if (p / sub).is_dir() and any((p / sub).iterdir()):
                    shutil.copytree(p / sub, dst / sub, dirs_exist_ok=True)
    sanitize_tree(dst)
    for p in run_dirs:  # paths into the scratch run dir -> the copies next to this file
        _rewrite(dst, f"{work}/{p.name}/", "")
        _rewrite(dst, f"{work}/{p.name}", ".")
    console = (dst / "console.txt").read_text()
    return {"scenario": s.name, "proves": s.proves, "status": results[0]["status"] if results
            else "?", "exit": proc.returncode, "key": key_line(s, results, console) if results
            else "", "problems": check(s, proc.returncode, results)}


def _quote(arg: str) -> str:
    return f"'{arg}'" if any(c in arg for c in ' {}"') else arg


def write_catalog() -> None:
    """evidence/catalog/tools.json: what an agent sees (`cua catalog list --json`)."""
    out = subprocess.run([sys.executable, "-m", "cua.cli", "catalog", "list", "--json"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    (EVIDENCE / "catalog").mkdir(exist_ok=True)
    (EVIDENCE / "catalog" / "tools.json").write_text(sanitize_text(out))


def table(rows: list[dict]) -> str:
    lines = ["| # | Scenario | What it proves | Status | Exit | Key result |",
             "|---|---|---|---|---|---|"]
    for i, r in enumerate(rows, 1):
        key = r["key"].replace("|", "\\|")
        lines.append(f"| {i} | [`{r['scenario']}`](replay/{r['scenario']}/) | {r['proves']} | "
                     f"{r['status']} | {r['exit']} | {key} |")
    return "\n".join(lines)


def _existing_rows(block: str) -> dict[str, dict]:
    """Rows of the current README table (so a partial run keeps the others untouched)."""
    rows = {}
    for line in block.splitlines():
        cells = [c.strip() for c in re.split(r"(?<!\\)\|", line)[1:-1]]
        m = re.match(r"\[`([^`]+)`\]", cells[1]) if len(cells) == 6 else None
        if m:
            rows[m.group(1)] = {"scenario": m.group(1), "proves": cells[2], "status": cells[3],
                                "exit": cells[4], "key": cells[5].replace("\\|", "|")}
    return rows


def write_readme(rows: list[dict]) -> None:
    readme = EVIDENCE / "README.md"
    text = readme.read_text() if readme.exists() else f"# Evidence\n\n{START}\n{END}\n"
    if START not in text:
        text += f"\n{START}\n{END}\n"
    head, rest = text.split(START, 1)
    block, tail = rest.split(END, 1)
    merged = _existing_rows(block) | {r["scenario"]: r for r in rows}
    ordered = [merged[s.name] for s in SCENARIOS if s.name in merged]
    readme.write_text(f"{head}{START}\n{table(ordered)}\n{END}{tail}")


def main(selected: list[str]) -> int:
    scenarios = [s for s in SCENARIOS if not selected or s.name in selected]
    if not selected:
        shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True, exist_ok=True)
    for s in scenarios:
        shutil.rmtree(OUT / s.name, ignore_errors=True)
    tenants = {s.tenant for s in scenarios}
    apps = {t: App(t) for t in sorted(tenants)}
    rows = []
    try:
        for s in scenarios:
            t0 = time.monotonic()
            row = run_scenario(s, apps)
            rows.append(row)
            flag = "OK " if not row["problems"] else "BAD"
            print(f"{flag} {s.name:40} {row['status']:16} exit {row['exit']:<3} "
                  f"{time.monotonic() - t0:5.1f}s  {row['key']}"
                  + (f"  PROBLEMS: {row['problems']}" if row["problems"] else ""), flush=True)
    finally:
        for app in apps.values():
            app.stop()
    write_catalog()
    print()
    print(table(rows))
    write_readme(rows)
    bad = [r for r in rows if r["problems"]]
    if bad:
        print(f"\n{len(bad)} scenario(s) did not behave as expected", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
