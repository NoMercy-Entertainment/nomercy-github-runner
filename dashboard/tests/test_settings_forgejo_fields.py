# dashboard/tests/test_settings_forgejo_fields.py
"""Task 9 added FORGEJO_INSTANCE_URL, FORGEJO_API_TOKEN and
FORGEJO_RUNNER_LABELS to the settings allowlist (EDITABLE) and marked the
token a secret (SECRET_KEYS), but settings.html never grew fields for them -
they were writable only by a direct API call, not from the page an operator
actually uses.

FORGEJO_API_TOKEN mints registration tokens and deletes runners, so it is
exactly as sensitive as GH_TOKEN (runner_detail.SECRET_KEYS treats them
identically) and must get the same treatment on this page: a password input
that never carries the real value, a masked preview of what is on disk, and a
submission that leaves the stored secret alone when the box is left blank.
"""
import os
import re

TPL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "templates", "settings.html")


def _html():
    with open(TPL, encoding="utf-8") as fh:
        return fh.read()


def test_all_three_forgejo_fields_are_present():
    html = _html()
    for key in ("FORGEJO_INSTANCE_URL", "FORGEJO_API_TOKEN", "FORGEJO_RUNNER_LABELS"):
        assert f'id="{key}"' in html, key


def test_forgejo_api_token_gets_the_same_password_treatment_as_gh_token():
    html = _html()
    # Same input type as GH_TOKEN: the real value is never rendered.
    assert re.search(r'<input[^>]*type="password"[^>]*id="FORGEJO_API_TOKEN"', html), \
        "FORGEJO_API_TOKEN must be a password input, like GH_TOKEN"
    # Same masked-preview treatment as GH_TOKEN's token_mask.
    assert "forgejo_token_mask" in html


def test_forgejo_fields_are_saved_like_every_other_field():
    html = _html()
    m = re.search(r"const FIELDS = \[(.*?)\];", html, re.S)
    assert m, "FIELDS list not found"
    fields = m.group(1)
    for key in ("FORGEJO_INSTANCE_URL", "FORGEJO_API_TOKEN", "FORGEJO_RUNNER_LABELS"):
        assert f"'{key}'" in fields, key


def test_forgejo_api_token_box_is_cleared_after_save_like_gh_token():
    """GH_TOKEN's box is blanked after a successful save so a just-typed
    secret is not left sitting in the DOM. FORGEJO_API_TOKEN must get the
    identical treatment."""
    html = _html()
    assert "$('GH_TOKEN').value = ''" in html
    assert "$('FORGEJO_API_TOKEN').value = ''" in html
