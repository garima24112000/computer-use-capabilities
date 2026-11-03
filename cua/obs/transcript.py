"""Transcript: runs/<run_id>/transcript.jsonl, the full REDACTED model exchange of a discovery run.

One JSON object per line ({"kind": "system" | "request" | "response", ...}). Everything goes
through the Redactor before it is written. Artifacts store only the file's sha256, never its
content.
"""

import hashlib
import json
import threading
from typing import Any

from pydantic_core import to_jsonable_python

from cua.obs.run import RunContext
from cua.policy.redaction import Redactor


class TranscriptWriter:
    def __init__(self, run: RunContext, redactor: Redactor):
        self.path = run.dir / "transcript.jsonl"
        self.redactor = redactor
        self._lock = threading.Lock()
        self._fh = open(self.path, "a", encoding="utf-8")  # noqa: SIM115

    def write(self, kind: str, **data: Any) -> None:
        record = self.redactor.redact({"kind": kind, **to_jsonable_python(data)})
        with self._lock:
            self._fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def sha256(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()
