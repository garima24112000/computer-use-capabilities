"""Fixtures: target app servers in background threads, and (logged-in) Playwright surfaces."""

import httpx
import pytest

from cua.surface.web import PlaywrightSurface
from target_app.background import serve_in_thread
from tests.surface.helpers import login


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


@pytest.fixture
def surface(app_a, make_surface) -> PlaywrightSurface:
    s = make_surface()
    login(s, app_a)
    return s


@pytest.fixture
def surface_b(app_b, make_surface) -> PlaywrightSurface:
    s = make_surface()
    login(s, app_b)
    return s


def set_faults(base_url: str, **faults) -> None:
    httpx.post(f"{base_url}/__faults", json=faults).raise_for_status()
