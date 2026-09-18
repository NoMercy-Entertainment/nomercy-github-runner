"""The Windows Forgejo template keeps the same promises as the Linux unit.

infra/windows/templates/<name>/ is what the Windows runtime copies into a
runner's `reg` directory at create (agent/runtimes/windows_process.py): its
three entry points, beside the traceably built forgejo-runner.exe. Each
promise here is one the Linux unit keeps too (test_unit_image.py), for the
same reasons.
"""
import os
import re

from agent.runtimes.windows_process import TEMPLATE_FILES

TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "infra", "windows", "templates",
    "forgejo-runner-v13.1.0-windows")


def read(name):
    with open(os.path.join(TEMPLATE, name), encoding="utf-8") as fh:
        return fh.read()


def test_it_has_every_entry_point_the_runtime_copies():
    assert sorted(os.listdir(TEMPLATE)) == sorted(TEMPLATE_FILES)


def test_the_runner_runs_with_a_shutdown_timeout():
    """Unset, forgejo-runner cancels its jobs the moment it is signalled -
    and the drain's signal is the job host's Ctrl+Break."""
    run = read("run.cmd")
    assert "shutdown_timeout: 3h" in run
    assert re.search(r'daemon --config "%~dp0config.yaml"', run)


def test_it_waits_for_the_registration_without_a_console():
    run = read("run.cmd")
    assert 'if exist ".runner" goto run' in run
    assert "timeout /t" not in run, "a service has no console for it"


def test_register_answers_with_the_forges_ids_and_is_idempotent():
    reg = read("register.ps1")
    assert "[Console]::In.ReadToEnd()" in reg, "the plan comes on stdin"
    assert re.search(r"if \(Test-Path .*'\.runner'\)\) \{ Answer; exit 0 \}",
                     reg)
    assert "registration_id" in reg and "registration_uuid" in reg


def test_register_never_echoes_the_token_in_an_error():
    assert "-replace [regex]::Escape([string]$plan.token), '***'" in \
        read("register.ps1")


def test_deregister_fails_while_registered():
    dereg = read("deregister.ps1")
    assert re.search(r"^exit 3$", dereg, re.M)


def test_register_is_bounded_and_leaves_nothing_running():
    """forgejo-runner pings an unreachable instance for ever; the agent
    gives up after 120 s. Measured 2026-09-18: stopped at 91 s, exit 124,
    no process left, the token nowhere in what it said."""
    reg = read("register.ps1")
    assert "WaitForExit(90000)" in reg and "$p.Kill()" in reg
