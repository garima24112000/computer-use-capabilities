"""Shared fixtures: target app servers in background threads, surfaces, guarded surfaces."""

from types import SimpleNamespace

import httpx
import pytest

from cua.obs.events import EventLogger
from cua.obs.run import RunContext
from cua.policy.guard import PolicyGuard
from cua.policy.guarded_surface import GuardedSurface
from cua.policy.models import PolicyConfig, PolicyContext
from cua.policy.redaction import Redactor
from cua.surface.web import PlaywrightSurface
from target_app.background import serve_in_thread


@pytest.fixture(scope="session")
def _server_a():
    with serve_in_thread("a") as url:
        yield url


@pytest.fixture(scope="session")
def _server_b():
    with serve_in_thread("b") as url:
        yield url


@pytest.fixture
def app_a(_server_a) -> str:
    httpx.post(f"{_server_a}/__faults/reset").raise_for_status()
    return _server_a


@pytest.fixture
def app_b(_server_b) -> str:
    httpx.post(f"{_server_b}/__faults/reset").raise_for_status()
    return _server_b


@pytest.fixture
def make_surface():
    created: list[PlaywrightSurface] = []

    def factory(**kwargs) -> PlaywrightSurface:
        s = PlaywrightSurface(headless=True, **kwargs)
        s.start()
        created.append(s)
        return s

    yield factory
    for s in created:
        s.close()


@pytest.fixture(scope="session")
def policy() -> PolicyConfig:
    return PolicyConfig.load()


@pytest.fixture
def make_guarded(make_surface, policy, tmp_path):
    """Factory for a GuardedSurface wired to a fresh RunContext under tmp_path/runs."""
    loggers: list[EventLogger] = []

    def factory(mode="discovery", approver=None, artifact_approved=False, step_approved=False,
                **surface_kwargs) -> SimpleNamespace:
        redactor = Redactor(policy.sensitive_labels)
        run = RunContext("discovery" if mode == "discovery" else "replay", redactor,
                         root=tmp_path / "runs", goal="test", tenant="a")
        logger = EventLogger(run, redactor)
        loggers.append(logger)
        ctx = PolicyContext(mode=mode, run_id=run.run_id, artifact_approved=artifact_approved,
                            step_approved=step_approved)
        guarded = GuardedSurface(make_surface(**surface_kwargs), PolicyGuard(policy), lambda: ctx,
                                 logger, approver=approver, redactor=redactor)
        return SimpleNamespace(surface=guarded, run=run, logger=logger, redactor=redactor,
                               ctx=ctx)

    yield factory
    for logger in loggers:
        logger.close()
