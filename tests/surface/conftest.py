"""Surface fixtures: logged-in Playwright surfaces (servers come from tests/conftest.py)."""

import pytest

from cua.surface.web import PlaywrightSurface
from tests.surface.helpers import login


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
