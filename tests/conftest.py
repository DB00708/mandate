import os
import sys

import httpx
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.family import API, PAYPAL, Family  # noqa: E402


def _stack_up() -> bool:
    try:
        return httpx.get(f"{API}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def pytest_collection_modifyitems(config, items):
    if _stack_up():
        return
    skip = pytest.mark.skip(reason=f"Mandate stack not reachable at {API}")
    for item in items:
        if "unit" not in str(item.fspath):
            item.add_marker(skip)


@pytest.fixture
def sharma():
    """A freshly onboarded Sharma household (isolated per test)."""
    fam = Family().onboard()
    yield fam
    httpx.delete(f"{PAYPAL}/_faults")
    assert fam.ledger_ok(), "hash chain must verify after every scenario"


@pytest.fixture
def family_factory():
    made = []

    def make(**kw):
        f = Family().onboard(**kw)
        made.append(f)
        return f
    yield make
    httpx.delete(f"{PAYPAL}/_faults")
    for f in made:
        assert f.ledger_ok()
