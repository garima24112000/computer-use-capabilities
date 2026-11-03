"""Dev helper: sign on, open member 12345's detail page and print what the LLM will see.

Uses only the Surface API (TargetSpecs built by hand), exactly like replay will.
"""

import argparse
import os

from cua.surface import FrameRef, LabelAnchor, RoleName, TargetSpec
from cua.surface.web import PlaywrightSurface
from target_app.background import serve_in_thread

TOP, NAV, MAIN = FrameRef(), FrameRef(name_path=["nav"]), FrameRef(name_path=["main"])


def spec(frame: FrameRef, description: str, *strategies) -> TargetSpec:
    return TargetSpec(frame=frame, strategies=list(strategies), description=description)


def run(base_url: str, member: str, headed: bool) -> None:
    with PlaywrightSurface(headless=not headed) as s:
        s.open(f"{base_url}/login")
        s.fill(spec(TOP, "operator id", LabelAnchor(label="OPERATOR ID", role="textbox")),
               os.environ.get("BANK_OPERATOR_USER", "demo_operator"))
        s.fill(spec(TOP, "password", LabelAnchor(label="PASSWORD", role="textbox")),
               os.environ.get("BANK_OPERATOR_PASS", "demo-pass-123"))
        s.click(spec(TOP, "sign on", RoleName(role="button", name="SIGN ON")))
        s.click(spec(NAV, "menu: member inquiry", RoleName(role="button", name="MEMBER INQUIRY")))
        s.fill(spec(MAIN, "member no", LabelAnchor(label="MEMBER NO.", role="textbox")), member)
        s.click(spec(MAIN, "inquire", RoleName(role="link", name="INQUIRE")))
        obs = s.observe()
        name_link = next(e for e in obs.elements
                         if e.role == "link" and e.table_context
                         and e.table_context.column_header == "NAME")
        s.click(name_link.ref)
        print(s.observe().to_prompt_text())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="use an already-running target app")
    parser.add_argument("--member", default="12345")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    if args.url:
        run(args.url, args.member, args.headed)
    else:
        with serve_in_thread("a") as url:
            run(url, args.member, args.headed)


if __name__ == "__main__":
    main()
