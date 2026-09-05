"""The bug: removing the last Forgejo runner hid the whole Forgejo section -
heading, "+ Add runner", all of it - leaving the owner with no control that
could ever bring one back. Forgejo CI jobs queued for two days with nothing
able to pick them up, because the one button that could have created a
runner had vanished along with the runners.

The fix has two halves:
  - docker_ops.collect() now says which providers are actually CONFIGURED
    (`providers_configured`), not just which have runners right now -
    "zero runners" and "never configured" used to look identical to the
    page, and only the second one should hide anything.
  - index.html keeps a configured-but-empty section visible, with its
    "+ Add runner" button showing (that is precisely what an empty fleet
    needs), while "Recreate fleet" and "Clear all cache" still hide - they
    have nothing to act on with zero runners, same as before.

Read test_two_sections.py and test_elsewhere_section.py first - this file
follows their idiom: plain string/regex assertions against the template's
JS for the front-end half, and real docker_ops/app calls for the back-end
half. There is no JS runtime in this suite, so the front-end assertions
pin the exact lines the behaviour depends on rather than executing them.
"""
import os

import pytest

import docker_ops
import providers

import app as dash

TPL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "index.html")


def _html():
    with open(TPL, encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture(autouse=True)
def _clear_forge_status_cache():
    """Same reasoning as test_forgejo_job_state.py / test_elsewhere_section.py:
    collect()'s Forgejo answer is cached in a module global, and leaving it
    warm would make these tests depend on execution order."""
    docker_ops._forge_status_cache = None
    yield
    docker_ops._forge_status_cache = None


@pytest.fixture(autouse=True)
def _isolated_state_file(tmp_path, monkeypatch):
    """collect() persists Forgejo uuid bookkeeping to docker_ops.STATE_PATH -
    point it at a fresh temp file so nothing leaks between tests."""
    monkeypatch.setattr(docker_ops, "STATE_PATH", str(tmp_path / "state.json"))


def _github_only_docker(*args, **kwargs):
    """No Forgejo container on this engine at all - the last one was just
    removed, or none was ever created. This is exactly the shape the owner
    was stuck in."""
    if args[:3] == ("ps", "-a", "--format"):
        return (True, "github-runner-1\t\n", "")
    if args[:3] == ("ps", "-a", "--filter"):
        return (True, "Up 1 hour", "")
    if args and args[0] == "stats":
        return (True, "", "")
    return (True, "", "")


class _Forge:
    def runner_statuses(self):
        return []


# --------------------------------------------------------------------------
# docker_ops.collect(): the signal the page needs did not exist before
# --------------------------------------------------------------------------

def test_a_configured_forgejo_with_zero_runners_is_still_configured(
        monkeypatch):
    """The exact situation from the bug report: the last Forgejo runner is
    gone, but FORGEJO_INSTANCE_URL/FORGEJO_API_TOKEN are still set. Zero
    runners must not read as unconfigured."""
    monkeypatch.setattr(docker_ops, "_docker", _github_only_docker)
    monkeypatch.setattr(providers.FORGEJO, "forge_client",
                        lambda env: _Forge())

    status = docker_ops.collect({})

    assert status["providers_configured"] == {"github": True, "forgejo": True}


def test_an_unconfigured_forgejo_reports_false(monkeypatch):
    """forge_client(env) returning None - no URL/token in env - is the only
    thing that should turn this off; it is the same cheap, network-free
    check the Elsewhere gate already relies on."""
    monkeypatch.setattr(docker_ops, "_docker", _github_only_docker)
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: None)

    status = docker_ops.collect({})

    assert status["providers_configured"] == {"github": True, "forgejo": False}


def test_github_is_always_configured_even_with_no_github_runners(
        monkeypatch):
    """GitHub carries no configuration gate on the Fleet page - it is always
    reported usable, regardless of Forgejo's state or of how many GitHub
    runners currently exist."""
    def no_runners_at_all(*args, **kwargs):
        if args[:3] == ("ps", "-a", "--format"):
            return (True, "", "")
        return (True, "", "")
    monkeypatch.setattr(docker_ops, "_docker", no_runners_at_all)
    monkeypatch.setattr(providers.FORGEJO, "forge_client", lambda env: None)

    status = docker_ops.collect({})

    assert status["providers_configured"]["github"] is True


# --------------------------------------------------------------------------
# app.py: the new key must survive the WebSocket diff path, not just HTTP
# --------------------------------------------------------------------------

def _snap(providers_configured, generated="t0"):
    return {"generated": generated, "runners": [],
            "providers_configured": providers_configured}


def test_the_diff_loop_knows_about_the_new_key():
    """diff_status compares a fixed set of keys whole-value; a key missing
    from that set never reaches a live subscriber until their next full
    poll. This is the exact class of bug the Elsewhere section already hit
    once (see test_elsewhere_markup.py's
    test_diff_status_propagates_elsewhere_over_the_socket) - same guard,
    new key."""
    import re
    app_py = os.path.join(os.path.dirname(os.path.dirname(TPL)), "app.py")
    with open(app_py, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r'for key in \(([^)]*)\):\s*\n\s*if \(old or \{\}\)\.get\(key\)',
                 src)
    assert m, "diff_status's whole-value comparison loop was not found"
    assert "providers_configured" in m.group(1)


def test_a_provider_becoming_configured_is_reported_over_the_socket():
    """End to end through the real function, not just a source-text check:
    Settings turning Forgejo credentials on mid-session must show up as a
    delta the next time the collector ticks."""
    before = _snap({"github": True, "forgejo": False})
    after = _snap({"github": True, "forgejo": True})

    delta = dash.diff_status(before, after)

    assert delta["providers_configured"] == {"github": True, "forgejo": True}


def test_an_unchanged_providers_configured_is_not_reported():
    """Matches the "an idle fleet is silent" property the rest of the diff
    already has - a key that has not changed must not show up in the delta
    just because it exists."""
    snap = _snap({"github": True, "forgejo": True})
    assert dash.diff_status(snap, dict(snap)) is None


# --------------------------------------------------------------------------
# index.html: the section stays up, Add stays, Recreate/prune still hide
# --------------------------------------------------------------------------

def _render_body():
    html = _html()
    start = html.index("function render(d) {")
    end = html.index("function setStale(on)")
    return html[start:end]


def test_section_visibility_is_driven_by_configured_not_runner_count():
    """The line the whole bug hinges on: display used to be gated on
    `list.length`. It must now be gated on whether the provider is
    configured, so a configured-but-empty fleet stays visible."""
    body = _render_body()
    assert "fleetEl.style.display = configured ? '' : 'none';" in body
    assert "grid.closest('.fleet').style.display = list.length" not in body


def test_configured_is_read_from_the_payload_and_github_is_hardcoded_true():
    body = _render_body()
    assert "d.providers_configured" in body
    assert "key === 'github' || !!providersConfigured[key]" in body


def test_recreate_and_prune_hide_on_an_empty_fleet_but_add_does_not():
    """Recreate and Clear-all-cache still have nothing to act on with zero
    runners and must keep hiding. The Add button must NOT be part of that
    same hasRunners-gated block - it is the one action an empty, configured
    fleet still needs, which is the whole point of this fix."""
    body = _render_body()
    assert "const hasRunners = list.length > 0;" in body
    assert "recreateBtn.style.display = hasRunners ? '' : 'none';" in body
    assert "pruneBtn.style.display = hasRunners ? '' : 'none';" in body
    # The block that gates Recreate/Prune on hasRunners must not also touch
    # the Add button - Add has no visibility toggle at all in render().
    gate_start = body.index("const hasRunners = list.length > 0;")
    gate_end = body.index("const skel = grid.querySelector")
    gate_block = body[gate_start:gate_end]
    assert "btn-add-" not in gate_block


def test_an_unconfigured_provider_still_hides_its_whole_section():
    """The half of the original behaviour that must survive: a deployment
    where Forgejo has no URL/token at all grows no Forgejo heading."""
    body = _render_body()
    assert "const configured = key === 'github' || !!providersConfigured[key];" in body
    assert "fleetEl.style.display = configured ? '' : 'none';" in body


def test_the_add_button_markup_lives_inside_the_forgejo_section():
    """Guards the premise of the whole fix: the Add button has to be inside
    the section that the visibility toggle controls, or keeping the section
    up would not actually restore the owner's only way to recover."""
    html = _html()
    section = html[html.index('data-provider="forgejo"'):]
    section = section[:section.index("</div>\n\n<div class=\"disk\"")]
    assert 'id="btn-add-forgejo"' in section


def test_empty_state_copy_mentions_adding_a_runner():
    """A bare "No runners" answers nothing; the line has to actually point
    at the control that fixes it."""
    body = _render_body()
    assert "use + Add runner" in body


def test_applydelta_carries_the_new_key_over_the_socket():
    """Mirrors applyDelta's existing handling of disk/host/elsewhere - a key
    the diff loop sends but applyDelta ignores never reaches state, and the
    page would only ever see it after a full page reload."""
    html = _html()
    fn = html[html.index("function applyDelta"):html.index("function transport")]
    assert "if (d.providers_configured) s.providers_configured = d.providers_configured;" in fn
