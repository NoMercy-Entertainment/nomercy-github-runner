"""Which job a unit is running, read from what its runner prints.

Neither forge says it through the API the controller reads: GitHub marks a
runner busy and Forgejo marks it active, and that is all. The runner says
more in its own output - "Running job: test / coverage", "task 412 repo is
NoMercy/app" - and that output is the same whatever kind of unit the runner
lives in, a container's log, a file on a Windows worker, a log in a guest.
So the reading is here once, and each runtime only has to say where the text
is.

The rules are the ones the dashboard this platform replaces arrived at. The
last event of either kind decides: grepping only for "Running job" reports a
finished job as running for ever once its completion has scrolled out of
the tail.

Display only. Whether a runner is busy is the forge's word, never this:
nothing here is evidence that a runner is idle, and nothing destructive may
be decided on it.
"""
import re

#: As long as a card can show.
MAX_NAME = 200

_GITHUB_STARTED = re.compile(r"Running job:\s*(.+?)\s*$")
_GITHUB_FINISHED = re.compile(r"Job .* completed with result")
_FORGEJO_TASK = re.compile(r'msg="task (\d+) repo is ([^\s"]+)')


def current_job(text):
    """The job the runner's output says is running now, or None."""
    running = None
    for line in (text or "").splitlines():
        started = _GITHUB_STARTED.search(line)
        if started:
            running = started.group(1)
            continue
        if _GITHUB_FINISHED.search(line):
            running = None
            continue
        task = _FORGEJO_TASK.search(line)
        if task:
            running = f"task {task.group(1)} - {task.group(2)}"
    return running[:MAX_NAME] if running else None
