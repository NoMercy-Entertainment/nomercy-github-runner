"""The Linux unit image keeps the promises the agent relies on.

The scripts in images/linux/unit/runner/ are the other half of the Linux
runtime: the agent mounts the layout, runs `register` and `deregister` in the
unit, and drains by SIGTERM. Each promise below was broken once by something
that looked reasonable - a start script that stops waiting for its runner
after two seconds, a forgejo-runner started without a shutdown timeout - and
this is where it is pinned, so it cannot drift from the runtime unnoticed.

They were also run for real, in throwaway units on the WSL engine, on
2026-09-18: the evidence file records a drain that let a job finish with
SIGTERM sent twice, and a GitHub registration whose token was on no argument
list.
"""
import os
import re
import shutil
import subprocess

import pytest

from agent.runtimes.linux_container import LAYOUT_ENV, MOUNTS

UNIT = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "images", "linux", "unit")


def read(*parts):
    with open(os.path.join(UNIT, *parts), encoding="utf-8") as fh:
        return fh.read()


def branch(script, kind):
    """The body of one forge's `case` arm in a script."""
    m = re.search(rf"^\s*{kind}\)\n(.*?)^\s*;;", script, re.S | re.M)
    assert m, f"no {kind} branch"
    return m.group(1)


class TestTheLayoutIsTheRuntimes:
    def test_the_defaults_are_the_mounts_the_agent_makes(self):
        lib = read("runner", "lib.sh")
        for name, path in LAYOUT_ENV.items():
            assert f'{name}="${{{name}:-{path}}}"' in lib, name
        assert MOUNTS["docker"] == "/var/lib/docker"

    def test_each_image_says_which_forge_it_serves(self):
        assert "RUNNER_KIND=github" in read("Dockerfile.github")
        assert "RUNNER_KIND=forgejo" in read("Dockerfile.forgejo")
        for name in ("Dockerfile.github", "Dockerfile.forgejo"):
            assert 'ENTRYPOINT ["/runner/run"]' in read(name)


class TestTheDrain:
    def test_forgejo_runs_with_a_shutdown_timeout(self):
        """Unset, forgejo-runner cancels its jobs the moment it is
        signalled; the drain would abort the job it exists to save."""
        forgejo = branch(read("runner", "run"), "forgejo")
        assert "shutdown_timeout: 3h" in forgejo
        assert re.search(r'daemon --config "\$RUNNER_REG_DIR/config.yaml"',
                         forgejo)

    def test_the_entry_point_waits_for_its_runner_without_a_limit(self):
        """PID 1 exiting ends every process in the unit. The start scripts
        the fleet runs today give up after two seconds."""
        run = read("runner", "run")
        signal_handler = re.search(r"on_signal\(\) \{(.*?)\n\}", run,
                                   re.S).group(1)
        assert "exit" not in signal_handler, \
            "the signal handler must not end the unit"
        assert 'kill -TERM "$RUNNER_PID"' in signal_handler
        assert re.search(r'while :; do\n\s+if wait "\$RUNNER_PID"', run)

    def test_the_github_runner_is_passed_the_signal(self):
        assert "RUNNER_MANUALLY_TRAP_SIG=1" in branch(read("runner", "run"),
                                                     "github")


class TestTheToken:
    def test_github_takes_it_from_the_environment_not_the_command_line(
            self):
        github = branch(read("runner", "register"), "github")
        assert 'ACTIONS_RUNNER_INPUT_TOKEN="$(field token)"' in github
        assert "--token" not in github

    def test_the_plan_is_read_from_standard_input(self):
        assert 'plan="$(cat)"' in read("runner", "register")


class TestDeregistration:
    def test_a_registered_unit_says_it_cannot_and_fails(self):
        """Exit 0 would tell the controller the record is gone, and it
        would be stranded at the forge."""
        dereg = read("runner", "deregister")
        assert re.search(r"^exit 3$", dereg, re.M)
        unregistered = re.search(r"^if ! registered; then\n(.*?)^fi$", dereg,
                                 re.S | re.M)
        assert unregistered and "exit 0" in unregistered.group(1), \
            "only an unregistered unit succeeds"


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
@pytest.mark.parametrize("script", ["run", "register", "deregister",
                                    "lib.sh"])
def test_every_script_parses(script):
    subprocess.run(["bash", "-n", os.path.join(UNIT, "runner", script)],
                   check=True)
