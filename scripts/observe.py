"""Dev helper: sign on, open member 12345's detail page and print what the LLM will see.

Runs through the full guarded stack (policy + redaction + event log) inside a RunContext, so it
also leaves a sample runs/<run_id>/events.jsonl. The printed text is the REDACTED observation:
exactly what discovery will hand to the model.
"""

import argparse
import os

from dotenv import load_dotenv

from cua.obs.events import EventLogger
from cua.obs.evidence import EvidenceCapture
from cua.obs.run import RunContext
from cua.policy.guard import DenyAllApprover, PolicyGuard
from cua.policy.guarded_surface import GuardedSurface
from cua.policy.models import PolicyConfig, PolicyContext
from cua.policy.redaction import Redactor
from cua.surface import FrameRef, LabelAnchor, RoleName, TableCell, TargetSpec
from cua.surface.web import PlaywrightSurface
from target_app.background import serve_in_thread

TOP, NAV, MAIN = FrameRef(), FrameRef(name_path=["nav"]), FrameRef(name_path=["main"])


def spec(frame: FrameRef, description: str, *strategies) -> TargetSpec:
    return TargetSpec(frame=frame, strategies=list(strategies), description=description)


def run(base_url: str, member: str, headed: bool) -> None:
    policy = PolicyConfig.load()
    redactor = Redactor(policy.sensitive_labels)
    ctx_run = RunContext("discovery", redactor, goal=f"observe member {member} detail",
                         tenant="a")
    logger = EventLogger(ctx_run, redactor)
    ctx = PolicyContext(mode="discovery", run_id=ctx_run.run_id)
    logger.emit("run_started", "system", goal=ctx_run.manifest.goal)

    with PlaywrightSurface(headless=not headed) as inner:
        s = GuardedSurface(inner, PolicyGuard(policy), lambda: ctx, logger,
                           approver=DenyAllApprover(), redactor=redactor)
        evidence = EvidenceCapture(s, ctx_run, redactor, policy, logger, actor="agent")
        s.open(f"{base_url}/login")
        s.fill(spec(TOP, "operator id", LabelAnchor(label="OPERATOR ID", role="textbox")),
               os.environ.get("BANK_OPERATOR_USER", "demo_operator"))
        s.fill(spec(TOP, "password", LabelAnchor(label="PASSWORD", role="textbox")),
               "{{secret:BANK_OPERATOR_PASS}}")
        s.click(spec(TOP, "sign on", RoleName(role="button", name="SIGN ON")))
        s.click(spec(NAV, "menu: member inquiry", RoleName(role="button", name="MEMBER INQUIRY")))
        s.fill(spec(MAIN, "member no", LabelAnchor(label="MEMBER NO.", role="textbox")), member)
        s.click(spec(MAIN, "inquire", RoleName(role="link", name="INQUIRE")))
        s.click(spec(MAIN, "member name link", TableCell(row_key=member, column_header="NAME")))
        obs = s.observe()
        evidence.capture_step("member detail")
        print(obs.to_prompt_text())

    logger.emit("run_finished", "system")
    ctx_run.finish("completed")
    logger.close()
    print(f"\n# run dir: {ctx_run.dir}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="use an already-running target app")
    parser.add_argument("--member", default="12345")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    load_dotenv()
    os.environ.setdefault("BANK_OPERATOR_PASS", "demo-pass-123")  # fake demo credential
    if args.url:
        run(args.url, args.member, args.headed)
    else:
        with serve_in_thread("a") as url:
            run(url, args.member, args.headed)


if __name__ == "__main__":
    main()
