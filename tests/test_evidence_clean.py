"""evidence/ and capabilities/ hold no secrets, PII, AWS keys, local paths or huge files."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"


def test_evidence_and_capabilities_are_clean():
    if not EVIDENCE.exists() or not any(p.is_file() for p in EVIDENCE.rglob("*")):
        pytest.skip("evidence/ is empty")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "scan_evidence.py")],
                         capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr


def test_scanner_catches_planted_values(tmp_path):
    (tmp_path / "a.jsonl").write_text('{"x": "Demo_Operator 900-55-5555 FIVES, FIONA E"}\n')
    (tmp_path / "b.png").write_bytes(b"\x89PNG demo-pass-123")
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "scan_evidence.py"),
                          str(tmp_path)], capture_output=True, text=True, cwd=ROOT)
    assert out.returncode == 1
    for label in ("BANK_OPERATOR_USER", "55555.ssn", "55555.name", "BANK_OPERATOR_PASS"):
        assert label in out.stdout
