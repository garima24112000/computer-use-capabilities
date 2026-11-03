"""EventLogger and RunContext unit tests (no browser)."""

import json

from cua.obs.events import EventLogger
from cua.obs.run import RunContext
from cua.policy.models import PolicyConfig
from cua.policy.redaction import Redactor


def make(tmp_path):
    redactor = Redactor(PolicyConfig.load().sensitive_labels)
    run = RunContext("discovery", redactor, root=tmp_path, goal="look up 900-12-3456",
                     tenant="a", model_id="test-model")
    return run, EventLogger(run, redactor), redactor


def test_run_context_layout_and_manifest(tmp_path):
    run, logger, _ = make(tmp_path)
    assert run.dir.parent == tmp_path and run.screenshots_dir.is_dir()
    assert run.snapshots_dir.is_dir()
    manifest = json.loads((run.dir / "manifest.json").read_text())
    assert manifest["run_id"] == run.run_id and manifest["kind"] == "discovery"
    assert manifest["goal"] == "look up [REDACTED:SSN]"  # manifests are redacted too
    assert manifest["finished_at"] is None
    run.finish("completed")
    manifest = json.loads((run.dir / "manifest.json").read_text())
    assert manifest["status"] == "completed" and manifest["finished_at"]
    assert not run.trace_path.exists()  # tracing is opt-in
    logger.close()


def test_events_are_monotonic_valid_jsonl_and_redacted(tmp_path):
    run, logger, redactor = make(tmp_path)
    redactor.register_secret("demo-pass-123")
    logger.emit("run_started", goal="x")
    logger.emit("action", "agent", "s1", value="demo-pass-123", note="SSN 900-12-3456")
    logger.emit("outcome", "agent", nested={"phone": ["(555) 555-0101"], "amount": "$12,345.67"})
    logger.emit("run_finished")
    logger.close()

    lines = run.events_path.read_text().splitlines()
    events = [json.loads(line) for line in lines]
    assert [e["seq"] for e in events] == [1, 2, 3, 4]
    assert {e["run_id"] for e in events} == {run.run_id}
    assert events[1]["actor"] == "agent" and events[1]["step_id"] == "s1"
    assert events[1]["data"] == {"value": "[REDACTED:SECRET]", "note": "SSN [REDACTED:SSN]"}
    assert events[2]["data"]["nested"] == {"phone": ["[REDACTED:PHONE]"],
                                           "amount": "$12,345.67"}
    raw = run.events_path.read_text()
    assert "demo-pass-123" not in raw and "900-12-3456" not in raw


def test_events_are_flushed_per_event(tmp_path):
    run, logger, _ = make(tmp_path)
    logger.emit("run_started")
    assert run.events_path.read_text().count("\n") == 1  # visible before close()
    logger.close()
