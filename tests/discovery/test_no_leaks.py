"""Data-handling guarantees for discovery: nothing secret or PII in the run dir, the artifact,
or anything sent to the model."""

import json

from tests.discovery.conftest import read_balance_script

FORBIDDEN = [
    "demo-pass-123",                       # operator password (secret)
    "900-12-3456",                         # fake SSN
    "(555) 555-0101", "555-0101",          # fake phone
    "john.sample@example.com",             # fake email
    "SAMPLE, JOHN Q",                      # member name
    "100 TEST LANE",                       # address
]


def test_run_dir_and_artifact_contain_no_secrets_or_pii(app_a, discover):
    d = discover(app_a, read_balance_script())
    assert d.result.status == "completed"
    files = [p for p in d.run_dir.rglob("*") if p.is_file()]
    names = {p.name for p in files}
    assert {"transcript.jsonl", "events.jsonl", "summary.json", "manifest.json"} <= names
    files.append(d.tmp / "capabilities" / "meridian.member.read_savings_balance" / "1.0.0.json")
    for path in files:
        blob = path.read_bytes()
        for needle in FORBIDDEN:
            assert needle.encode() not in blob, f"{needle!r} leaked into {path}"
    transcript = (d.run_dir / "transcript.jsonl").read_text()
    assert "[REDACTED:SSN]" in transcript and "$12,345.67" in transcript  # useful, not leaky


def test_prompts_sent_to_the_model_have_no_credentials_or_pii(app_a, discover):
    d = discover(app_a, read_balance_script())
    sent = []
    for system, messages, tools in d.llm.calls:
        sent.append(system)
        sent += [m.model_dump_json() for m in messages]
        sent += [json.dumps(t.model_dump()) for t in tools]
    blob = "\n".join(sent)
    for needle in FORBIDDEN + ["BANK_OPERATOR_PASS", "BANK_OPERATOR_USER", "{{secret:"]:
        assert needle not in blob, f"{needle!r} was sent to the model"
    assert "demo_operator" not in blob.lower()  # the operator id is echoed upper-cased
    assert "[REDACTED:NAME]" in blob and "{{inputs.member_id}}" in blob
    system = d.llm.calls[0][0]
    assert "Look up member 12345" in system  # non-sensitive goal text is kept


def test_sensitive_input_values_are_never_shown_or_stored(app_a, discover):
    inputs = [{"name": "member_id", "value": "31415", "type": "string",
               "pattern": r"^\d{5}$", "sensitive": True}]
    d = discover(app_a, read_balance_script(), inputs=inputs, member="31415")
    assert d.result.status == "completed" and d.result.outputs == {"savings_balance": "3141.59"}
    blob = "\n".join(m.model_dump_json() for _, ms, _ in d.llm.calls for m in ms)
    blob += "\n".join(s for s, _, _ in d.llm.calls)
    assert "31415" not in blob and "[REDACTED:INPUT]" in blob
    artifact = d.tmp / "capabilities" / "meridian.member.read_savings_balance" / "1.0.0.json"
    for path in [*d.run_dir.rglob("*"), artifact]:
        if path.is_file():
            assert b"31415" not in path.read_bytes(), path
    # A sensitive input may not key a table row, so the name link can only be located
    # structurally. The artifact is honest about it: css_path only, flagged by lint.
    name_link = d.cap.steps[3].action.target
    assert [s.kind for s in name_link.strategies] == ["css_path"]
    assert "brittle_target" in d.cap.lint().codes()
    assert d.cap.inputs[0].sensitive and d.cap.inputs[0].example is None
