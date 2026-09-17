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


def test_elsewhere_cards_stay_read_only():
    """These runners are not containers on this engine, so nothing on their
    card may offer to act on one.

    _elsewhere() never feeds ops.list_runner_names(), so a button here could
    not work even if it were wired up; the guarantee is that none is drawn.

    Build cache stays out for the original reason: it is read with
    `docker exec` against a container that does not exist for these. CPU,
    memory and disk no longer are - they come from the exporter on the host
    (see test_external_telemetry.py), which is why the meters below are now
    expected rather than forbidden."""
    html = _html()
    fn = html[html.index("function makeElseCard"):]
    fn = fn[:fn.index("function render(d)")]
    for forbidden in ("build_cache", 'data-a="stop"', 'data-a="remove"',
                      'data-a="prune"', 'data-a="drain"', 'data-a="restart"'):
        assert forbidden not in fn, f"Elsewhere card must not carry {forbidden}"


def test_elsewhere_cards_carry_the_same_meters_as_the_rest():
    """The point of the exporter: these cards stop being blank next to every
    other runner on the page."""
    html = _html()
    fn = html[html.index("function makeElseCard"):]
    fn = fn[:fn.index("function render(d)")]
    for meter in ("meterHTML('cpu'", "meterHTML('mem'", "meterHTML('disk'"):
        assert meter in fn, f"missing {meter}"


def test_absent_telemetry_reads_unknown_rather_than_zero():
    """A confident 0% would render an unreachable machine as an idle one."""
    html = _html()
    assert "setMeter(card, 'cpu', 'unknown', 0)" in html
    assert "setMeter(card, 'disk', 'unknown', 0)" in html


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
