import pytest

from agent.cpu import ceiling, cpuset_count


@pytest.mark.parametrize("cpuset,count", [("0-15", 16), ("0-3,2-5,2", 6),
    ("9-2", 0), ("-2", 0), ("a", 0), ("0,2,4", 3)])
def test_unique_allowed_cores(cpuset, count):
    assert cpuset_count(cpuset) == count


@pytest.mark.parametrize("quota", ["nan", "inf", "-inf", "garbage"])
def test_invalid_quota_is_not_a_numeric_ceiling(quota):
    assert ceiling("0-3", quota) == 4


def test_fractional_quota_is_preserved():
    assert ceiling("0-3", 1500000000) == 1.5
