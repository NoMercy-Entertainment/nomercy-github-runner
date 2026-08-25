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


def test_deregistration_survives_shutdown():
    """The daemon is started as a background child and waited on, not exec'd.
    A trap handler does not survive exec — the shell's process image is replaced.
    Keeping this shell alive as PID 1 is what makes deregistration reachable when
    the container stops. This pattern is documented in scripts/start.sh:447-452."""
    start = _read(START)
    # The daemon is not exec'd (which would replace the shell and lose traps)
    assert "exec forgejo-runner daemon" not in start
    # A TERM trap exists to deregister
    assert "trap" in start and "deregister" in start
    # The script waits on the runner child rather than exiting immediately
    assert "wait" in start and "RUNNER_PID" in start


def test_the_child_is_signalled_on_shutdown():
    """Without signalling the child, `docker stop` takes the full grace period
    (~60s) waiting for the daemon to exit on its own. Every stop would be slow,
    and the dashboard's remove button would appear frozen. The stop_runner()
    function must send kill -TERM to the child to unblock the wait loop."""
    start = _read(START)
    # The script must send SIGTERM to the child
    assert "kill -TERM \"$RUNNER_PID\"" in start
    # And it must do so with error suppression (child may already be gone)
    assert "2>/dev/null || true" in start
    # stop_runner function must exist and be called
    assert "stop_runner()" in start
    assert "stop_runner" in start


def test_signal_handlers_carry_their_own_exit_code():
    """A single handler shared across INT/TERM/EXIT that hard-coded `exit 0`
    always reported success regardless of which signal fired, and lost the
    real code entirely (measured: SIGTERM shutdowns exited 0 instead of 143).
    INT and TERM must each bind their own instance of shutdown_handler,
    passing the signal name (for the log line) and the real exit code."""
    start = _read(START)
    assert "trap 'shutdown_handler INT 130' INT" in start
    assert "trap 'shutdown_handler TERM 143' TERM" in start


def test_the_exit_trap_does_not_call_the_exiting_handler():
    """Binding shutdown_handler itself to EXIT is the re-entrancy bug this
    file shipped twice: bash runs the EXIT trap after any trap's `exit`, so a
    shutdown_handler bound to EXIT re-enters itself on every signal-driven
    shutdown (measured: its "shutting down runner" line printed twice, the
    second time after the child was already dead). EXIT must bind to a
    separate, non-exiting fallback for the no-signal path instead."""
    start = _read(START)
    assert "trap 'cleanup_on_exit' EXIT" in start
    assert "trap 'shutdown_handler' EXIT" not in start
    assert "trap 'shutdown_handler'" not in start
