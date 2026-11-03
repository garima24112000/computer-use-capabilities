"""Mutable per-app state: operator sessions and deterministic, counter-based fault injection."""

import secrets
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import BaseModel, Field


class FaultState(BaseModel):
    """Pending faults. Counters decrement once per affected main-frame request."""

    latency_ms: int = Field(default=0, ge=0)
    latency_count: int = Field(default=0, ge=0)
    interstitial_count: int = Field(default=0, ge=0)
    js_confirm_count: int = Field(default=0, ge=0)
    http500_count: int = Field(default=0, ge=0)
    http500_path_prefix: str = "/"


class FaultUpdate(BaseModel):
    """Body of POST /__faults. Only the fields provided are changed."""

    latency_ms: int | None = Field(default=None, ge=0)
    latency_count: int | None = Field(default=None, ge=0)
    interstitial_count: int | None = Field(default=None, ge=0)
    js_confirm_count: int | None = Field(default=None, ge=0)
    http500_count: int | None = Field(default=None, ge=0)
    http500_path_prefix: str | None = None
    expire_session_now: bool = False


@dataclass
class Session:
    operator: str
    last_seen: float


class SessionStore:
    """Idle-timeout sessions keyed by an opaque cookie value."""

    def __init__(self, ttl_seconds: float, clock: Callable[[], float]):
        self.ttl = ttl_seconds
        self.clock = clock
        self._sessions: dict[str, Session] = {}

    def create(self, operator: str) -> str:
        sid = secrets.token_hex(16)
        self._sessions[sid] = Session(operator=operator, last_seen=self.clock())
        return sid

    def get(self, sid: str | None) -> Session | None:
        """Return the live session and refresh its idle timer, or None if unknown/expired."""
        if not sid or sid not in self._sessions:
            return None
        session = self._sessions[sid]
        now = self.clock()
        if now - session.last_seen > self.ttl:
            del self._sessions[sid]
            return None
        session.last_seen = now
        return session

    def drop(self, sid: str | None) -> None:
        self._sessions.pop(sid or "", None)

    def expire_all(self) -> None:
        self._sessions.clear()
