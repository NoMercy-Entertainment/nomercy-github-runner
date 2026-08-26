"""Destructive actions get a real dialog, not window.confirm().

window.confirm() cannot be styled, cannot say which button is the dangerous
one, is suppressible by the browser after repeated use, and renders as a
system alert that looks nothing like the rest of the page. Every destructive
action here - stop, remove, recreate, clear cache - went through it.

These tests are the regression guard: the next confirm() typed into a
template fails the suite instead of quietly shipping.
"""
import glob
import os
import re

TEMPLATES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")

# window.confirm( and !confirm( both match; confirmDialog( deliberately
# does not, because "confirm" there is followed by "D", not "(".
RE_NATIVE_CONFIRM = re.compile(r"\bconfirm\s*\(")


def test_no_template_calls_window_confirm():
    offenders = []
    for path in sorted(glob.glob(os.path.join(TEMPLATES, "*.html"))):
        with open(path, encoding="utf-8") as fh:
            for n, line in enumerate(fh, 1):
                if RE_NATIVE_CONFIRM.search(line):
                    offenders.append(f"{os.path.basename(path)}:{n}")
    assert offenders == [], f"native confirm() left in: {offenders}"


def test_base_defines_the_dialog_element_and_helper():
    """One dialog in the shared layout, so all four pages get the same one."""
    with open(os.path.join(TEMPLATES, "base.html"), encoding="utf-8") as fh:
        src = fh.read()
    assert "<dialog" in src, "base.html must own the dialog element"
    assert "confirmDialog" in src, "base.html must expose the confirmDialog helper"


def test_every_page_that_confirms_uses_the_shared_helper():
    """The pages with destructive buttons must actually call it."""
    for name in ("index.html", "runner.html", "settings.html"):
        with open(os.path.join(TEMPLATES, name), encoding="utf-8") as fh:
            assert "confirmDialog(" in fh.read(), f"{name} does not confirm anything"


def test_the_dialog_centres_itself_despite_the_global_reset():
    """The reset zeroes every margin, including the one that centres a modal.

    A browser centres <dialog> by giving it margin:auto. base.html resets
    margin to 0 on *, which silently drops the dialog into the top-left
    corner - correct-looking CSS, wrong result, and invisible until you open
    one.
    """
    with open(os.path.join(TEMPLATES, "base.html"), encoding="utf-8") as fh:
        src = fh.read()

    assert "margin:0" in src, "the reset this guards against is gone; drop this test"

    start = src.index("dialog#confirm{")
    rule = src[start:src.index("}", start)]
    assert "margin:auto" in rule, "the dialog does not restore its own centring"


def test_the_layout_declares_a_favicon():
    """Without an explicit icon every page load also 404s /favicon.ico."""
    with open(os.path.join(TEMPLATES, "base.html"), encoding="utf-8") as fh:
        src = fh.read()
    assert 'rel="icon"' in src
    assert "image/svg+xml" in src
