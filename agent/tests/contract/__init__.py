"""The conformance suite: design 19.2's nine scenarios, once per runtime.

`suite.py` holds the scenarios and is written only against `Harness`, the
small interface each platform provides in `harnesses.py`. A runtime joins the
suite by adding a harness to `HARNESSES`; nothing in the scenarios changes.
That order is the point of writing it now, before the Windows and macOS
runtimes exist (risk R-6): they will be held to this, rather than this being
written to whatever they turn out to do.
"""
