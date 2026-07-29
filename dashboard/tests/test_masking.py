import runner_detail


def test_mask_keeps_ends_hides_middle():
    out = runner_detail.mask("ghp_abcdefghijklmnopqrstuvwxyz1234")
    assert out == "ghp_" + "•" * 8 + "1234"


def test_mask_short_value_is_fully_hidden():
    assert runner_detail.mask("abc") == "***"
    assert runner_detail.mask("12345678") == "*" * 8


def test_mask_empty_is_empty():
    assert runner_detail.mask("") == ""
    assert runner_detail.mask(None) == ""


def test_secret_keys_contains_the_token():
    assert "GH_TOKEN" in runner_detail.SECRET_KEYS
