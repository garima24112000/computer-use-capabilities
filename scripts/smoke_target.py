"""Smoke test: drive Meridian Core in headless Chromium; print member 12345's savings balance.

Starts the target app on a free port (unless --url is given), signs on, navigates the frameset
and reads the balance from the ACCOUNTS table. Exits non-zero on any failure.
"""

import argparse
import os
import socket
import subprocess
import sys
import time

import httpx
from playwright.sync_api import Frame, Page, sync_playwright

USER = os.environ.get("BANK_OPERATOR_USER", "demo_operator")
PASS = os.environ.get("BANK_OPERATOR_PASS", "demo-pass-123")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(url: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{url}/login", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    raise RuntimeError(f"target app at {url} did not become ready in {timeout}s")


def frame(page: Page, name: str) -> Frame:
    found = page.frame(name=name)
    if found is None:
        raise RuntimeError(f"frame {name!r} not found; frames={[f.name for f in page.frames]}")
    return found


def run(url: str, member: str) -> str:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
        page.on("dialog", lambda d: d.accept())

        page.goto(f"{url}/login")
        page.locator("xpath=//td[normalize-space()='OPERATOR ID:']/following-sibling::td//input"
                     ).fill(USER)
        page.locator("xpath=//td[normalize-space()='PASSWORD:']/following-sibling::td//input"
                     ).fill(PASS)
        with page.expect_navigation():
            page.locator("div", has_text="SIGN ON").click()

        nav = frame(page, "nav")
        nav.wait_for_load_state()
        main = frame(page, "main")
        with main.expect_navigation():
            nav.get_by_text("MEMBER INQUIRY").click()

        main.locator("xpath=//td[normalize-space()='MEMBER NO.']/following-sibling::td//input"
                     ).fill(member)
        with main.expect_navigation():
            main.get_by_text("INQUIRE", exact=True).click()
        with main.expect_navigation():
            main.locator("a[href^='javascript:openMbr']").first.click()

        balance = main.locator(
            "xpath=//tr[td[normalize-space()='Share Savings']]/td[3]"
        ).first.inner_text()
        browser.close()
        return balance.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="use an already-running target app instead of starting one")
    parser.add_argument("--member", default="12345")
    args = parser.parse_args()

    server = None
    url = args.url
    if url is None:
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        server = subprocess.Popen(
            [sys.executable, "-m", "target_app", "--port", str(port), "--tenant", "a"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        wait_ready(url)
        balance = run(url, args.member)
        print(f"SMOKE OK: member {args.member} Share Savings balance = {balance}")
        return 0
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
