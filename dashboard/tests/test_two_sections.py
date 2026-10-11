# dashboard/tests/test_two_sections.py
"""Each fleet gets its own grid and its own buttons.

The point is not cosmetic. "Recreate fleet" and "Clear all cache" are
destructive and fleet-specific; a single global button above a mixed grid
leaves it ambiguous which runners it is about to take out.
"""
import os
import re

TPL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "index.html")


def _html():
    with open(TPL, encoding="utf-8") as fh:
        return fh.read()


def test_there_is_a_grid_per_provider():
    html = _html()
    assert 'id="grid-github"' in html
    assert 'id="grid-forgejo"' in html


def test_each_fleet_has_its_own_destructive_buttons():
    html = _html()
    for key in ("github", "forgejo"):
        assert f'data-provider="{key}"' in html
        assert f'id="btn-add-{key}"' in html
        assert f'id="btn-recreate-{key}"' in html


def test_every_fleet_action_names_its_provider():
    """A POST to add or recreate without a provider is a 400 by design, so a
    button that omits it is a button that cannot work."""
    html = _html()
    for m in re.finditer(r"/api/(runner/add|recreate)", html):
        window = html[max(0, m.start() - 400):m.start() + 400]
        assert "provider" in window, m.group(0)


def test_the_counters_stay_fleet_wide():
    html = _html()
    assert 'id="s-online"' in html
    assert 'id="s-busy"' in html
