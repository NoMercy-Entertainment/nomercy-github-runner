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
import subprocess

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

    def test_the_guard_actually_refuses_here(self):
        """This test host is not a container, so the script must exit non-zero
        and must not have written anything."""
        for path in (START, START_FORGEJO):
            out = subprocess.run(["bash", path], capture_output=True, text=True)
            assert out.returncode != 0, f"{path} did not refuse"
            assert "REFUSING" in out.stderr, out.stderr[:200]
