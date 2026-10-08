"""The host engine must survive a restart, and the entrypoints must stay in
their containers.

Two failures on 2026-09-17 motivate this file.

A container wedged in "removal already in progress" could only be cleared by
restarting dockerd, and dockerd could not be restarted because live-restore
was off and seven builds were running. The daemon was therefore unrestartable
for as long as the fleet was busy - which is most of the time, and exactly
when a stuck container is most likely.

Separately, scripts/start.sh writes /etc/docker/daemon.json. Inside a
container that is the nested daemon's config; run on the distro it is the HOST
engine's config, which now carries live-restore. Overwriting it and killing
that dockerd takes the whole fleet down, and has.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
INSTALL = os.path.join(ROOT, "scripts", "install-docker.sh")
START = os.path.join(ROOT, "scripts", "start.sh")
START_FORGEJO = os.path.join(ROOT, "scripts", "start-forgejo.sh")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


class TestTheDaemonCanBeRestarted:
    def test_provisioning_enables_live_restore(self):
        assert '"live-restore": true' in _read(INSTALL)

    def test_it_reloads_rather_than_restarts(self):
        """A restart to enable the setting that makes restarts safe would stop
        every container - the exact outage it exists to prevent. live-restore
        is one of the few options dockerd applies on SIGHUP."""
        install = _read(INSTALL)
        assert "systemctl reload docker" in install

    def test_an_existing_config_is_merged_not_overwritten(self):
        """The distro's daemon.json is not the file the entrypoints write, and
        clobbering it has taken the fleet down before."""
        install = _read(INSTALL)
        assert 'if [ -f /etc/docker/daemon.json ]' in install
        assert "json.load" in install, "an existing file must be read, not replaced"


class TestTheEntrypointsStayInTheirContainers:
    def test_both_refuse_to_run_outside_one(self):
        for path in (START, START_FORGEJO):
            src = _read(path)
            assert "/.dockerenv" in src, f"{path} has no guard"
            assert "REFUSING" in src, f"{path} does not say why"

    def test_the_guard_runs_before_anything_touches_the_system(self):
        """A guard after the first side effect guards nothing.

        Comment lines are stripped first: the guard's own explanation names
        the files it protects, and matching that text would pass for the
        wrong reason.
        """
        for path in (START, START_FORGEJO):
            code = "\n".join(
                ln for ln in _read(path).splitlines()
                if not ln.lstrip().startswith("#"))
            guard = code.index("/.dockerenv")
            for danger in ("/etc/docker/daemon.json", "dockerd ", "pkill"):
                if danger in code:
                    assert guard < code.index(danger), \
                        f"{path}: guard comes after {danger!r}"

    @staticmethod
    def _guard(path, tmp_path, dockerenv, unit, ci):
        """Run only the guard block, with /.dockerenv and /runner/run pointed
        at files this test controls. Never the whole script: on 2026-10-08
        this test ran start.sh itself inside github-linux-x64-9, where the
        old guard let it through, and it killed that runner's engine."""
        code = _read(path)
        block = code[code.index("# --- guard ---"):code.index("# --- end guard ---")]
        envfile, runfile = tmp_path / "dockerenv", tmp_path / "run"
        for wanted, f in ((dockerenv, envfile), (unit, runfile)):
            if wanted:
                f.write_text("")
            elif f.exists():
                f.unlink()
        block = (block.replace("/.dockerenv", envfile.as_posix())
                      .replace("/runner/run", runfile.as_posix()))
        script = tmp_path / "guard.sh"
        script.write_text(block + "\necho PASSED\n", newline="\n")
        env = {k: v for k, v in os.environ.items() if k != "GITHUB_ACTIONS"}
        if ci:
            env["GITHUB_ACTIONS"] = "true"
        return subprocess.run(["bash", script.as_posix()], capture_output=True,
                              text=True, env=env)

    @pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
    @pytest.mark.parametrize("dockerenv,unit,ci", [
        (False, False, False),      # a host
        (True, True, False),        # inside a platform unit
        (True, False, True),        # inside a CI job's container
    ])
    def test_the_guard_refuses_everywhere_but_its_own_container(
            self, tmp_path, dockerenv, unit, ci):
        for path in (START, START_FORGEJO):
            out = self._guard(path, tmp_path, dockerenv, unit, ci)
            assert out.returncode != 0, f"{path} did not refuse"
            assert "REFUSING" in out.stderr and "PASSED" not in out.stdout

    @pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
    def test_the_guard_lets_its_own_container_through(self, tmp_path):
        for path in (START, START_FORGEJO):
            out = self._guard(path, tmp_path, True, False, False)
            assert out.returncode == 0 and "PASSED" in out.stdout, out.stderr
