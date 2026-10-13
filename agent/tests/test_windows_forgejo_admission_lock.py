import ntpath

from agent.runtimes.windows_process import WindowsProcessRuntime, service_sid
from .fake_windows import FakeWindows, TOOLS


def test_guard_binary_launcher_and_policy_deny_writes_by_the_job_account():
    host = FakeWindows()
    runtime = WindowsProcessRuntime(run=host, fs=host, tools=TOOLS)
    rid = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
    paths = runtime.paths(rid)
    host.makedirs(paths["reg"])
    leaves = ("unit.json", "forgejo-runner.exe", "run.cmd")
    for leaf in leaves:
        host.put(ntpath.join(paths["reg"], leaf), 1)
    name = "rnr-" + rid
    runtime._lock_from_jobs(name, paths, False)
    denials = [c for c in host.calls if c[0] == TOOLS["icacls"] and "/deny" in c]
    assert {c[1] for c in denials} == {ntpath.join(paths["reg"], leaf) for leaf in leaves}
    assert all("*" + service_sid(name) + ":(" + runtime.DENY_WRITE + ")" in c for c in denials)
