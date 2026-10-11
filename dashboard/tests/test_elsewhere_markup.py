# dashboard/tests/test_elsewhere_markup.py
"""The Elsewhere section's markup: no buttons, hidden when empty, and a
sentence explaining why - not a disabled button, an absent one.

Mirrors the reasoning in test_two_sections.py: this is not cosmetic. The
Forgejo section carries "Recreate fleet" and "Clear all cache", and a card
sitting under those buttons implies they apply to it - so Elsewhere is a
separate section, below both forges, with none of its own.
"""
import os
import re

TPL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "index.html")


def _html():
    with open(TPL, encoding="utf-8") as fh:
        return fh.read()


def test_the_section_exists_below_both_forges():
    html = _html()
    gh = html.index('id="fleet-github"') if 'id="fleet-github"' in html \
        else html.index('data-provider="github"')
    fj = html.index('data-provider="forgejo"')
    el = html.index('id="fleet-elsewhere"')
    assert gh < fj < el, "Elsewhere must render after both forge sections"


def test_the_heading_says_elsewhere():
    html = _html()
    section = html[html.index('id="fleet-elsewhere"'):]
    section = section[:section.index('</div>\n\n<div class="disk"')]
    assert '>Elsewhere<' in section


def test_the_section_has_its_own_grid():
    assert 'id="grid-elsewhere"' in _html()


def test_the_section_carries_no_action_buttons():
    """No Add/Recreate/Clear-cache buttons, and no per-card actions row -
    this section is read-only, full stop."""
    html = _html()
    section = html[html.index('id="fleet-elsewhere"'):]
    section = section[:section.index('</div>\n\n<div class="disk"')]
    assert "<button" not in section
    assert "fleet-actions" in section, "the explanatory sentence still lives in one"


def test_the_section_explains_why_there_are_no_buttons():
    """A disabled button raises a question; an absent one with a sentence
    answers it - the sentence has to actually be there."""
    html = _html()
    section = html[html.index('id="fleet-elsewhere"'):]
    section = section[:section.index('</div>\n\n<div class="disk"')]
    assert "not containers on this engine" in section
    assert "cannot be started, stopped, or pruned" in section


def test_elsewhere_cards_are_keyed_by_uuid_not_name():
    """Forgejo documents runner names as not unique - a name-keyed Map could
    silently merge two different runners that happen to share one."""
    html = _html()
    assert "elseCards.get(r.uuid)" in html
    assert "elseCards.set(r.uuid" in html


def test_elsewhere_section_is_hidden_when_empty_like_the_other_two():
    html = _html()
    assert "$('fleet-elsewhere').style.display = elsewhere.length ? '' : 'none';" \
        in html


def test_elsewhere_omits_the_container_only_metrics():
    """CPU/memory/build cache/uptime/job all depend on `docker stats` and
    `docker exec` against a container that does not exist for these - the
    card-building code must never reference them."""
    html = _html()
    fn = html[html.index("function makeElseCard"):]
    fn = fn[:fn.index("function render(d)")]
    for forbidden in ("meterHTML", "cpu_percent", "mem_used", "build_cache",
                      'data-a="stop"', 'data-a="remove"', 'data-a="prune"'):
        assert forbidden not in fn


def test_elsewhere_card_name_is_not_a_link():
    """There is no /runner/<name> page for something that is not a
    container on this engine - and providers.valid_name() would 404 it even
    if there were a link."""
    html = _html()
    fn = html[html.index("function makeElseCard"):]
    fn = fn[:fn.index("function render(d)")]
    assert "<a " not in fn
    assert "/runner/" not in fn


def test_diff_status_propagates_elsewhere_over_the_socket():
    """Without this the section would only ever update via full polling,
    defeating the point of the WebSocket push for the two other sections."""
    app_py = os.path.join(os.path.dirname(os.path.dirname(TPL)), "app.py")
    with open(app_py, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r'for key in \(([^)]*)\):\s*\n\s*if \(old or \{\}\)\.get\(key\)',
                 src)
    assert m, "diff_status's whole-value comparison loop was not found"
    assert "elsewhere" in m.group(1)
