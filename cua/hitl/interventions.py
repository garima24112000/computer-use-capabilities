"""Interventions: requests for a human, and the store shared by the run and console processes.

The store is SQLite (stdlib) in WAL mode, so the run process and the operator console can use
it at the same time. Every status change is a compare-and-set
(`UPDATE ... WHERE id=? AND status=?`), so two operators cannot both claim an intervention.
Everything written is already redacted by the caller (the HitlSession owns a Redactor).
"""

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

Kind = Literal["discovery_stuck", "replay_failure", "approval_required", "commit_uncertain"]
Status = Literal["open", "claimed", "resolved", "expired"]
Resolution = Literal["resumed", "handled", "aborted", "approved", "denied"]
DEFAULT_DB = Path("runs") / "_hitl" / "interventions.db"


class LiveView(BaseModel):
    headed: bool
    cdp_url: str | None = None
    window_title: str | None = None


class HumanAction(BaseModel):
    """One thing the human did in the live session (already redacted)."""

    seq: int
    at: datetime
    operator: str
    type: Literal["click", "input", "select", "submit", "navigate"]
    frame: str
    role: str | None = None
    name: str | None = None
    detail: str = ""  # e.g. "value changed", option label, redacted value, URL path
    risk: str | None = None
    human_irreversible: bool = False

    def summary(self) -> str:
        what = f'{self.role} "{self.name}"' if self.name else self.frame
        flag = " [IRREVERSIBLE]" if self.human_irreversible else ""
        return f"{self.type} {what}{(': ' + self.detail) if self.detail else ''}{flag}"


class InterventionRequest(BaseModel):
    id: str = Field(default_factory=lambda: f"iv-{uuid.uuid4().hex[:10]}")
    run_id: str
    kind: Kind
    capability: str | None = None  # "id@version"
    goal: str | None = None  # redacted (discovery)
    step_id: str | None = None
    step_intent: str | None = None
    reason: str
    failure_category: str | None = None
    expected: str | None = None
    observed: str | None = None
    screenshot: str | None = None  # absolute path of the masked screenshot
    run_dir: str | None = None
    frame_urls: dict[str, str] = Field(default_factory=dict)  # frame key -> path only
    approval: dict[str, Any] | None = None  # PolicyDecision + action summary
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: Status = "open"
    operator: str | None = None
    resolution: Resolution | None = None
    operator_note: str | None = None
    human_actions: list[HumanAction] = Field(default_factory=list)
    live_view: LiveView = Field(default_factory=lambda: LiveView(headed=False))


class ControlSnapshot(BaseModel):
    run_id: str
    state: str
    owner: str
    epoch: int
    updated_at: datetime


class StaleState(Exception):
    """A compare-and-set lost: someone else changed the intervention first."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS interventions (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, status TEXT NOT NULL,
  created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS human_actions (
  intervention_id TEXT NOT NULL, seq INTEGER NOT NULL, data TEXT NOT NULL,
  PRIMARY KEY (intervention_id, seq));
CREATE TABLE IF NOT EXISTS control (
  run_id TEXT PRIMARY KEY, state TEXT NOT NULL, owner TEXT NOT NULL, epoch INTEGER NOT NULL,
  updated_at TEXT NOT NULL);
"""


class InterventionStore:
    def __init__(self, path: str | Path = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._conn().executescript(_SCHEMA)  # executescript manages its own transaction

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            self._local.conn = conn
        return conn

    @contextmanager
    def _tx(self):
        db = self._conn()
        db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise

    # --- interventions --------------------------------------------------------------------------

    def create(self, req: InterventionRequest) -> InterventionRequest:
        data = req.model_dump_json(exclude={"human_actions"})
        with self._tx() as db:
            db.execute("INSERT INTO interventions VALUES (?,?,?,?,?,?)",
                       (req.id, req.run_id, req.kind, req.status, req.created_at.isoformat(),
                        data))
        return req

    def get(self, iid: str) -> InterventionRequest | None:
        db = self._conn()
        row = db.execute("SELECT data, status FROM interventions WHERE id=?", (iid,)).fetchone()
        if row is None:
            return None
        req = InterventionRequest.model_validate_json(row[0])
        req.status = row[1]
        req.human_actions = [HumanAction.model_validate_json(r[0]) for r in db.execute(
            "SELECT data FROM human_actions WHERE intervention_id=? ORDER BY seq", (iid,))]
        return req

    def list(self, status: str | None = None, run_id: str | None = None
             ) -> list[InterventionRequest]:
        sql, args = "SELECT id FROM interventions WHERE 1=1", []
        if status:
            sql, args = sql + " AND status=?", [*args, status]
        if run_id:
            sql, args = sql + " AND run_id=?", [*args, run_id]
        ids = [r[0] for r in self._conn().execute(sql + " ORDER BY created_at DESC", args)]
        return [r for i in ids if (r := self.get(i)) is not None]

    def cas(self, iid: str, expect: set[str], **changes: Any) -> InterventionRequest:
        """Atomically apply `changes` if the current status is in `expect`, else StaleState."""
        with self._tx() as db:
            row = db.execute("SELECT data, status FROM interventions WHERE id=?",
                             (iid,)).fetchone()
            if row is None:
                raise KeyError(iid)
            if row[1] not in expect:
                raise StaleState(f"intervention {iid}: expected status in {sorted(expect)}; "
                                 f"observed {row[1]}")
            req = InterventionRequest.model_validate_json(row[0])
            req = req.model_copy(update=changes)
            cur = db.execute("UPDATE interventions SET status=?, data=? WHERE id=? AND status=?",
                             (req.status, req.model_dump_json(exclude={"human_actions"}), iid,
                              row[1]))
            if cur.rowcount != 1:
                raise StaleState(f"intervention {iid} changed concurrently")
        return self.get(iid)  # type: ignore[return-value]

    # operator commands (console / API) ----------------------------------------------------------

    def claim(self, iid: str, operator: str) -> InterventionRequest:
        return self.cas(iid, {"open"}, status="claimed", operator=operator)

    def resolve(self, iid: str, resolution: Resolution, operator: str | None = None,
                note: str | None = None) -> InterventionRequest:
        allowed = {"claimed"} if resolution == "resumed" else {"open", "claimed"}
        changes: dict[str, Any] = {"status": "resolved", "resolution": resolution,
                                   "operator_note": note}
        if operator:
            changes["operator"] = operator
        return self.cas(iid, allowed, **changes)

    def expire(self, iid: str) -> InterventionRequest:
        return self.cas(iid, {"open", "claimed"}, status="expired")

    def add_human_action(self, iid: str, action: HumanAction) -> None:
        with self._tx() as db:
            db.execute("INSERT INTO human_actions VALUES (?,?,?)",
                       (iid, action.seq, action.model_dump_json()))

    # --- control mirror (for the console) --------------------------------------------------------

    def set_control(self, run_id: str, state: str, owner: str, epoch: int) -> None:
        with self._tx() as db:
            db.execute("INSERT OR REPLACE INTO control VALUES (?,?,?,?,?)",
                       (run_id, state, owner, epoch, datetime.now(UTC).isoformat()))

    def control(self, run_id: str) -> ControlSnapshot | None:
        row = self._conn().execute("SELECT * FROM control WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            return None
        return ControlSnapshot(run_id=row[0], state=row[1], owner=row[2], epoch=row[3],
                               updated_at=row[4])

    def dump(self) -> "list[dict]":
        """Everything (for tests/audit)."""
        return [json.loads(r.model_dump_json()) for r in self.list()]
