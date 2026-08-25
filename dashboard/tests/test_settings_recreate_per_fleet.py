# dashboard/tests/test_settings_recreate_per_fleet.py
"""settings.html's per-fleet recreate buttons: the id, the visible label, and
the provider actually POSTed to /api/recreate must always agree.

A single global "Save & recreate fleet" button was correct as a Task 10
interim, when the settings page only held GitHub configuration. It stopped
being correct the moment Task 11 added the three Forgejo fields to that same
page: an operator editing FORGEJO_RUNNER_LABELS and clicking save-and-recreate
got nothing done to the Forgejo runners - the button silently acted on the
fleet the operator did not mean, which is the exact failure the two-section
design on the Fleet page exists to prevent, reappearing here.

The fix mirrors the Fleet page: one button per fleet, each carrying its own
provider. The obvious way to break it again is a copy-paste between the two
buttons that updates the id or the label but not data-provider (or vice
versa) - a button that says "recreate the Forgejo fleet" while its
data-provider (and therefore its POST body) still says "github".
"""
import os
import re

TPL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "settings.html")


def _html():
    with open(TPL, encoding="utf-8") as fh:
        return fh.read()


# One capture group per recreate button: the id suffix, the data-provider
# value, and the visible label text.
_BTN_RE = re.compile(
    r'<button id="save-recreate-(\w+)"[^>]*data-provider="(\w+)"[^>]*>([^<]*)</button>')


def test_there_is_a_recreate_button_per_provider():
    html = _html()
    ids = {m.group(1) for m in _BTN_RE.finditer(html)}
    assert ids == {"github", "forgejo"}, ids


def test_every_recreate_buttons_id_provider_and_label_all_agree():
    """The regression this guards: a copy-paste that updates the label or the
    id but not data-provider (or vice versa) would send the wrong fleet to
    /api/recreate while the button still claims to be the other one."""
    html = _html()
    matches = list(_BTN_RE.finditer(html))
    assert matches, "no per-fleet recreate buttons found in settings.html"
    for m in matches:
        id_suffix, provider, label = m.group(1), m.group(2), m.group(3)
        assert id_suffix == provider, \
            f"button id says '{id_suffix}' but data-provider says '{provider}': {m.group(0)}"
        # The visible label must name the same fleet the click handler will
        # actually send - reads b.dataset.provider and POSTs {provider}.
        assert provider in label.lower(), \
            f"label '{label}' does not name the fleet data-provider='{provider}' will POST"


def test_the_old_fleet_blind_button_is_gone():
    """id="save-recreate" (no per-fleet suffix) was the Task 10 interim - one
    button guessing which fleet to recreate. It must not come back."""
    html = _html()
    assert 'id="save-recreate"' not in html


def test_recreate_buttons_are_only_offered_for_fleets_that_have_runners(client, monkeypatch):
    """A fleet with nothing in it has nothing safe to recreate - same
    reasoning as the Fleet page hiding an empty section entirely."""
    import docker_ops
    import providers

    monkeypatch.setattr(docker_ops, "list_runners",
                         lambda: [("github-runner-1", providers.GITHUB)])
    html = client.get("/settings").get_data(as_text=True)
    assert 'id="save-recreate-github"' in html
    assert 'id="save-recreate-forgejo"' not in html

    monkeypatch.setattr(docker_ops, "list_runners",
                         lambda: [("forgejo-runner-1", providers.FORGEJO)])
    html = client.get("/settings").get_data(as_text=True)
    assert 'id="save-recreate-github"' not in html
    assert 'id="save-recreate-forgejo"' in html

    monkeypatch.setattr(docker_ops, "list_runners", lambda: [])
    html = client.get("/settings").get_data(as_text=True)
    assert 'id="save-recreate-github"' not in html
    assert 'id="save-recreate-forgejo"' not in html
