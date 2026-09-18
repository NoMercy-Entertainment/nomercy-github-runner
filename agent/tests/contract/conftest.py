"""The one fixture the suite needs: a fresh harness per runtime, per test."""
import pytest

from .harnesses import HARNESSES


@pytest.fixture(params=sorted(HARNESSES))
def harness(request):
    return HARNESSES[request.param]()
