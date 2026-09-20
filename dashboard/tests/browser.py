"""The headless browser the two rendering tests use, and whether it works.

Those tests exist because a page can be correct in the template and wrong in
the browser: the DOM they read back is the only place a `<template>` that is
never cloned, or a script that throws, shows up.

They already skip when no browser is installed. This adds the other case,
found on 2026-09-20: a browser that is installed, exits 0, and prints
nothing. Edge did that here after the host ran out of memory and Windows
started killing things, and it made two tests fail for a reason that had
nothing to do with the page.

So the browser is proved on a page whose DOM is known before it is asked
about ours. An empty answer to that control page means the browser cannot
render anything, which is a skip; an empty answer to our page while the
control renders is a real failure, and still fails.
"""
import os
import subprocess

#: Where a headless Chromium-family browser lives on this host.
CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/chromium", "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)

BROWSER = next((p for p in CANDIDATES if p and os.path.exists(p)), None)

CONTROL = ("<html><body><p id=\"rendered-by-the-control-page\">"
           "ok</p></body></html>")
MARKER = "rendered-by-the-control-page"


def dump_dom(url, profile, timeout=120):
    """The DOM the browser makes of `url`, or "" when it made none."""
    done = subprocess.run(
        [BROWSER, "--headless=new", "--disable-gpu", "--no-first-run",
         f"--user-data-dir={profile}", "--virtual-time-budget=5000",
         "--dump-dom", url],
        capture_output=True, text=True, timeout=timeout)
    return done.stdout or ""


def works(tmp_path):
    """Whether this browser renders at all, asked of a page we wrote."""
    if BROWSER is None:
        return False
    page = tmp_path / "control.html"
    page.write_text(CONTROL, encoding="utf-8")
    try:
        return MARKER in dump_dom(page.as_uri(), tmp_path / "edge-control")
    except (OSError, subprocess.SubprocessError):
        return False


def reason():
    return ("no headless browser installed" if BROWSER is None else
            f"{os.path.basename(BROWSER)} renders nothing on this host - it "
            f"answered a page of known content with an empty DOM")
