"""The API token is a credential and must be treated as one.

FORGEJO_API_TOKEN can mint registration tokens and delete runners. Rendering
it unmasked on a page anyone with dashboard access can open would hand that
over, and the token is one .env line away from GH_TOKEN, which is masked.
"""
import runner_detail


def test_the_api_token_is_masked():
    assert "FORGEJO_API_TOKEN" in runner_detail.SECRET_KEYS


def test_the_instance_url_is_not_a_secret():
    """It appears in the runner's own log lines. Masking it would only make
    the settings page harder to check without hiding anything."""
    assert "FORGEJO_INSTANCE_URL" not in runner_detail.SECRET_KEYS


def test_masking_leaves_a_token_recognisable_but_unusable():
    masked = runner_detail.mask("forgejo-abcdefghijklmnopqrstuvwxyz123456")
    assert "abcdefghijklmnop" not in masked


def test_the_settings_page_can_write_the_forgejo_keys():
    import app as dash
    for key in ("FORGEJO_INSTANCE_URL", "FORGEJO_API_TOKEN",
                "FORGEJO_RUNNER_LABELS"):
        assert key in dash.EDITABLE, key


def test_editable_is_still_an_allowlist():
    """A typo'd or injected key in .env is a foothold, not a cosmetic bug."""
    import app as dash
    assert "PATH" not in dash.EDITABLE
    assert "OIDC_CLIENT_SECRET" not in dash.EDITABLE
