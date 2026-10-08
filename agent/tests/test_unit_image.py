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
        assert "shutdown_timeout: 2562047h47m16s" in forgejo
        assert "timeout: 2562047h47m16s" in forgejo
        assert re.search(r'daemon --config "\$RUNNER_REG_DIR/config.yaml"',
                         forgejo)

    def test_forgejo_jobs_get_their_own_nested_docker_engine(self):
        forgejo = branch(read("runner", "run"), "forgejo")
        assert re.search(r"(?m)^container:\n(?:.*\n)*?  docker_host: automount$",
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

    def test_the_job_completed_hook_is_a_script_the_runner_accepts(self):
        """The GitHub runner runs a hook only when its path ends in .sh,
        .ps1 or .js; anything else fails the job's last step, "Complete
        runner", and with it the job. /runner/cleanup did exactly that to
        every GitHub job from 2026-09-21 to 2026-09-22 - 83 of 83."""
        hook = re.search(r"ACTIONS_RUNNER_HOOK_JOB_COMPLETED=(\S+)",
                         branch(read("runner", "run"), "github"))
        assert hook, "the github branch sets no completion hook"
        path = hook.group(1)
        assert path.endswith((".sh", ".ps1", ".js")), path
        assert os.path.exists(os.path.join(UNIT, "runner",
                                           os.path.basename(path)))
        assert os.path.basename(path) in read("Dockerfile.github")

    def test_the_job_started_hook_is_a_script_the_runner_accepts(self):
        """Same rule as the completion hook, and it ships in both the full
        image and the overlay the running fleet is refreshed with (#7, #13)."""
        hook = re.search(r"ACTIONS_RUNNER_HOOK_JOB_STARTED=(\S+)",
                         branch(read("runner", "run"), "github"))
        assert hook, "the github branch sets no job-started hook"
        path = hook.group(1)
        assert path.endswith(".sh"), path
        assert os.path.exists(os.path.join(UNIT, "runner", os.path.basename(path)))
        for dockerfile in ("Dockerfile.github", "Dockerfile.cleanup"):
            text = read(dockerfile)
            assert os.path.basename(path) in text and "job_started.py" in text, dockerfile

    def test_the_android_sdk_has_a_copy_no_job_deletes(self):
        """free-disk-space's `rm -rf /usr/local/lib/android` on a writable,
        long-lived root took the SDK from every runner it ran on (#13). A
        real copy, not hard links: a job rewriting an SDK file in place must
        not be able to change the copy it is restored from."""
        for dockerfile in ("Dockerfile.github", "Dockerfile.cleanup"):
            text = read(dockerfile)
            assert "cp -a /usr/local/lib/android /opt/nomercy/android-sdk.pristine" in text, dockerfile
        hook = read("runner", "job_started.py")
        assert "/opt/nomercy/android-sdk.pristine" in hook
        assert "/usr/local/lib/android" in hook


class TestTheToken:
    def test_github_takes_it_from_the_environment_not_the_command_line(
            self):
        github = branch(read("runner", "register"), "github")
        assert 'ACTIONS_RUNNER_INPUT_TOKEN="$(field token)"' in github
        assert "--token" not in github

    def test_the_plan_is_read_from_standard_input(self):
        assert 'plan="$(cat)"' in read("runner", "register")

    def test_a_plan_that_replaces_is_not_answered_from_the_volume(self):
        """A unit keeps its registration files on its volume, and the
        controller cannot make it drop them: `deregister` deliberately
        leaves them. So a unit whose record the controller has since
        deleted answered every later registration with the id of a record
        that no longer exists, and the wait for it to come online could
        only end at the deadline (2026-09-20). When the plan says replace,
        the unit registers.
        """
        reg = read("runner", "register")
        early = re.search(r"^if registered(.*?)^fi$", reg, re.S | re.M)
        assert early, "no early answer to guard"
        assert "replace" in early.group(1)

    def test_a_link_is_never_moved_onto_the_volume(self):
        """Two registrations of one unit can overlap - the controller
        retries an exec that has not answered - and the second would move
        the link the first had just made into the volume, leaving
        `/runner/reg/.credentials` pointing at itself. The runner reads
        that as "too many levels of symbolic links" and aborts, for ever
        (2026-09-20).
        """
        github = branch(read("runner", "register"), "github")
        moves = github.rsplit("for f in $REG_FILES; do", 1)[-1]
        assert "-L" in moves, \
            "the move must skip a file that is already a link"


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


class TestTheRunnerGitHubWillTalkTo:
    """GitHub refuses to deliver jobs to a deprecated runner: "Runner
    version v2.333.1 is deprecated and cannot receive messages", and the
    unit exits. The image a unit is built from ships whatever it ships -
    2.333.1 when this was written, while every runner that was serving had
    replaced it with 2.336.0 at start - so a rebuilt runner arrived dead
    (2026-09-20). The unit image pins the version, and the pin is the one
    the manifest records against the hash GitHub publishes.
    """

    def pins(self):
        df = read("Dockerfile.github")
        version = re.search(r"ARG RUNNER_VERSION=([\d.]+)", df)
        sha = re.search(r"ARG RUNNER_SHA256=([0-9a-f]{64})", df)
        assert version and sha, "the unit image pins no runner"
        return df, version.group(1), sha.group(1)

    def test_it_is_checked_against_that_hash_before_it_is_used(self):
        df, _, _ = self.pins()
        assert "sha256sum -c" in df

    def test_the_pin_is_what_the_manifest_records(self):
        import json
        _, version, sha = self.pins()
        images = os.path.dirname(os.path.dirname(UNIT))
        with open(os.path.join(images, "windows", "manifest.json"),
                  encoding="utf-8") as fh:
            entry = json.load(fh)["actions-runner-linux-x64"][0]
        assert (entry["version"], entry["sha256"]) == (version, sha)


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
@pytest.mark.parametrize("script", ["run", "register", "deregister",
                                    "lib.sh", "cleanup", "cleanup.sh",
                                    "job-started.sh", "maintenance"])
def test_every_script_parses(script):
    subprocess.run(["bash", "-n", os.path.join(UNIT, "runner", script)],
                   check=True)


def test_forgejo_registration_is_tried_again():
    """The instance answers its ping in about five seconds through the gate
    in front of it, and five seconds is where forgejo-runner gives up:
    "Cannot ping the Forgejo instance server ... context deadline
    exceeded". A rebuild that failed on it succeeded on the next attempt,
    so the unit makes that attempt itself (2026-09-20)."""
    forgejo = branch(read("runner", "register"), "forgejo")
    assert "until timeout 90" in forgejo
    assert re.search(r"attempt", forgejo)


def test_forgejo_registration_is_bounded():
    """It pings an unreachable instance for ever; the agent stops waiting
    after 120 s and would leave it running in the unit."""
    assert re.search(r"timeout 90 \"\$FORGEJO_RUNNER_BIN\" register",
                     branch(read("runner", "register"), "forgejo"))
