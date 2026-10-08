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
    assert "shutdown_timeout: 2562047h47m16s" in run
    assert "timeout: 2562047h47m16s" in run
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


TEMPLATES = os.path.dirname(TEMPLATE)


def _every(name):
    for template in sorted(os.listdir(TEMPLATES)):
        path = os.path.join(TEMPLATES, template, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                yield template, fh.read()


def test_the_wait_names_powershell_by_its_absolute_path():
    """The job host gives the runner no PATH. `powershell` alone was not
    found, so the wait for the registration never slept and spun one core
    at full load while it waited (2026-09-22)."""
    for template, run in _every("run.cmd"):
        waits = [line for line in run.splitlines()
                 if "Start-Sleep" in line and not line.lower().startswith("rem")]
        assert waits, template
        for line in waits:
            assert line.strip().startswith(
                r'"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"'), (template, line)


def test_register_keeps_the_exit_code_of_what_it_started():
    """Windows PowerShell loses a Start-Process -PassThru exit code unless
    the handle is taken first; the lost code read as $null and `exit $null`
    is 0, so a failed GitHub config.cmd answered success with no
    registration (2026-09-22)."""
    for template, reg in _every("register.ps1"):
        if "Start-Process" not in reg:
            continue
        assert re.search(r"\$null = \$\w+\.Handle", reg), template
