"""Core data-handling guarantee: nothing secret or PII lands anywhere under runs/<run_id>/.

Drives a realistic session (secret login, PII-heavy detail page, reads, describes, evidence
capture, a failure snapshot, and a deliberately PII-laden outcome event), then greps every byte
of every file in the run directory.
"""

import pytest

from cua.obs.evidence import EvidenceCapture
from cua.surface.locators import TableCell, TargetSpec
from tests.surface.helpers import MAIN, inquire, login, open_detail

FORBIDDEN = [
    "demo-pass-123",                       # operator password (secret)
    "900-12-3456",                         # fake SSN
    "(555) 555-0101", "555-0101",          # fake phone
    "john.sample@example.com",             # fake email
    "SAMPLE, JOHN Q",                      # member name
    "100 TEST LANE",                       # address
]


@pytest.fixture(autouse=True)
def _operator_secret(monkeypatch):
    monkeypatch.setenv("BANK_OPERATOR_PASS", "demo-pass-123")


def test_grep_run_dir_for_secrets_and_pii(app_a, make_guarded, policy):
    g = make_guarded()
    s = g.surface
    evidence = EvidenceCapture(s, g.run, g.redactor, policy, g.logger, actor="agent")
    g.logger.emit("run_started", "system", goal="read balance for member 12345")

    login(s, app_a, password="{{secret:BANK_OPERATOR_PASS}}")
    inquire(s, "12345")
    obs = s.observe()  # results page: name link
    name_ref = next(e.ref for e in obs.elements if e.role == "link"
                    and e.table_context and e.table_context.column_header == "NAME")
    g.logger.emit("checkpoint", "agent", target=s.describe(name_ref))
    evidence.capture_step("inquiry results")

    open_detail(s, "12345")
    obs = s.observe()
    for el in obs.elements:  # describe everything on the PII-heavy page
        if el.ref:
            g.logger.emit("checkpoint", "agent", target=s.describe(el.ref))
    balance = s.read_text(TargetSpec(frame=MAIN, description="balance", strategies=[
        TableCell(section_header="ACCOUNTS", row_key="Share Savings", column_header="BALANCE")]))
    g.logger.emit("outcome", "agent", balance=balance, frame_text=s.frame_text(MAIN),
                  raw_attempt="member SSN 900-12-3456 / (555) 555-0101")
    evidence.capture_step("member detail")
    evidence.capture_failure("demo failure snapshot")
    g.logger.emit("run_finished", "system")
    g.run.finish("completed")
    g.logger.close()

    files = [p for p in g.run.dir.rglob("*") if p.is_file()]
    names = {p.relative_to(g.run.dir).parts[0] for p in files}
    assert {"events.jsonl", "manifest.json", "screenshots", "snapshots"} <= names
    assert not (g.run.dir / "trace.zip").exists()
    assert len(list(g.run.screenshots_dir.glob("*.png"))) == 3

    for path in files:
        blob = path.read_bytes()
        for needle in FORBIDDEN:
            assert needle.encode() not in blob, f"{needle!r} leaked into {path}"

    events = g.run.events_path.read_text()
    assert "{{secret:BANK_OPERATOR_PASS}}" in events  # the template, not the value
    assert "$12,345.67" in events                      # business data still useful
    snapshot = next(g.run.snapshots_dir.glob("*.txt")).read_text()
    assert "[REDACTED:SSN]" in snapshot and "$12,345.67" in snapshot
