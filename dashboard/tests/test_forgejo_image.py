"""The properties of the Forgejo runner image the dashboard depends on.

Not a build test - building it takes minutes and needs the network. These are
the four things that, if they drift, break the dashboard silently rather than
loudly: the nested daemon (prune and the disk figures read it), the storage
driver (overlay-on-overlay does not mount), the build cache cap (a runner
filled a 1 TB disk once already), and the registration path providers.py reads.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

START = os.path.join(ROOT, "scripts", "start-forgejo.sh")
DOCKERFILE = os.path.join(ROOT, "forgejo-runner", "Dockerfile")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def test_the_runner_has_its_own_docker_daemon():
    assert "dockerd" in _read(START)


def test_the_daemon_uses_fuse_overlayfs():
    """The kernel cannot stack native overlay2 on the host's overlay."""
    assert "fuse-overlayfs" in _read(START)
    assert "fuse-overlayfs" in _read(DOCKERFILE)


def test_the_build_cache_is_capped():
    """Without a gc policy the nested daemons grow without limit. This is not
    theoretical - they once filled a 1 TB disk."""
    assert "builder" in _read(START) and "gc" in _read(START)


def test_registration_lands_where_providers_expects_it():
    import providers
    assert providers.FORGEJO.registration_path == "/data/.runner"
    start = _read(START)
    assert "/data" in start
    assert "forgejo-runner register" in start


def test_the_daemon_is_the_final_process():
    """exec, not a background start: the container must die when the runner
    does, or Docker reports Up for a runner that is gone."""
    assert "exec forgejo-runner daemon" in _read(START)
