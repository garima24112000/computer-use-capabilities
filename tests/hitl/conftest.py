"""HITL test harness. There is no real human; the tests play both roles:

- the OPERATOR, through the console's JSON API (a real uvicorn server on 127.0.0.1);
- the HUMAN, by attaching to the SAME browser over CDP (exactly what a person does with
  chrome://inspect) and driving the raw page, bypassing GuardedSurface, while they hold the lease.

The run under test (replay or discovery) runs in a background thread, because it blocks
inside the escalator while it waits for the operator.
"""

import json
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from cua.cli import app as cli_app
from cua.hitl.console import create_console
from cua.hitl.interventions import InterventionStore
from cua.surface.web.playwright_surface import _SharedDriver
from target_app.background import free_port

ROOT = Path(__file__).resolve().parents[2]
READ = "meridian.member.read_savings_balance"
OPEN = "meridian.subaccount.open"


@pytest.fixture(autouse=True)
def _operator_secrets(monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_USER", "demo_operator")
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")


def isolated_store(tmp_path: Path, cap_id: str, version: str, *, approve: bool = False) -> Path:
    """A private artifact store holding exactly `cap_id@version` (pinned; never depends on the
    approval state of the real capabilities/ directory)."""
    src = ROOT / "capabilities" / cap_id / f"{version}.json"
    if not src.exists():
        pytest.skip(f"{cap_id}@{version} not in capabilities/ (run `cua discover`)")
    root = tmp_path / "caps"
    (root / cap_id).mkdir(parents=True, exist_ok=True)
    data = json.loads(src.read_text())
    data["status"], data["review"] = "draft", {}
    for step in data["steps"]:
        step["approved"] = False
    dst = root / cap_id / f"{version}.json"
    dst.write_text(json.dumps(data))
    if approve:
        out = CliRunner().invoke(cli_app, ["artifact", "approve", str(dst), "--reviewer", "qa"])
        assert out.exit_code == 0, out.output
    return root


class Console:
    """The operator: a real console server plus helpers over its JSON API."""

    def __init__(self, base: str, store: InterventionStore):
        self.base, self.store = base, store
        self.http = httpx.Client(base_url=base, timeout=10)

    def list(self, status: str | None = None) -> list[dict]:
        return self.http.get("/api/interventions", params={"status": status} if status
                             else None).json()

    def get(self, iid: str) -> dict:
        return self.http.get(f"/api/interventions/{iid}").json()

    def cmd(self, iid: str, cmd: str, **body: Any) -> httpx.Response:
        return self.http.post(f"/api/interventions/{iid}/{cmd}", json=body)

    def wait_open(self, kind: str | None = None, timeout: float = 60, exclude=()) -> dict:
        def found():
            items = [i for i in self.list("open") if i["id"] not in exclude
                     and (kind is None or i["kind"] == kind)]
            return items[0] if items else None
        return wait_for(found, timeout, f"an open {kind or ''} intervention")

    def wait_control(self, iid: str, state: str, timeout: float = 30) -> dict:
        return wait_for(lambda: (c := self.get(iid)["control"]) and c["state"] == state and c,
                        timeout, f"control state {state}")


def wait_for(fn: Callable[[], Any], timeout: float, what: str) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def console(tmp_path):
    store = InterventionStore(tmp_path / "runs" / "_hitl" / "interventions.db")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(create_console(store, runs_root=tmp_path / "runs"),
                                           host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    wait_for(lambda: server.started, 10, "console server")
    c = Console(f"http://127.0.0.1:{port}", store)
    yield c
    c.http.close()
    server.should_exit = True
    thread.join(timeout=10)


class Background:
    """Runs `fn` in a thread (it has its own Playwright driver there) and keeps the result."""

    def __init__(self, fn: Callable[[], Any]):
        self.result: Any = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn):
        try:
            self.result = fn()
        except BaseException as exc:  # surfaced by join()
            self.error = exc

    def join(self, timeout: float = 120) -> Any:
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "run did not finish"
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def human():
    """Attach to the run's browser over CDP (from the test thread) and return its page."""
    attached: list = []

    def attach(cdp_url: str):
        pw = _SharedDriver.acquire()
        browser = pw.chromium.connect_over_cdp(cdp_url)
        attached.append(browser)
        # The run uses its own BrowserContext, not Chromium's default one: find the context
        # whose page shows the console frameset.
        context, page = next((c, pg) for c in browser.contexts for pg in c.pages
                             if pg.frame(name="main") is not None)
        return SimpleNamespace(browser=browser, context=context, page=page,
                               main=page.frame(name="main"), nav=page.frame(name="nav"),
                               top=page.frame(name="top"))

    yield attach
    if attached:
        _SharedDriver.release()


def cookie_digest(cookies: list[dict]) -> str:
    import hashlib

    pairs = sorted((c["name"], c["value"]) for c in cookies)
    return hashlib.sha256(repr(pairs).encode()).hexdigest()[:16]


def events(run_dir: str | Path) -> list[dict]:
    return [json.loads(line) for line in
            (Path(run_dir) / "events.jsonl").read_text().splitlines()]


def accounts_of(base_url: str, member: str) -> str:
    with httpx.Client(base_url=base_url) as http:
        http.post("/login", data={"f1": "demo_operator", "f2": "demo-pass-123"})
        page = http.get(f"/member/detail?m={member}").text
    return page[page.index("ACCOUNTS"):]


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, dirs_exist_ok=True)
