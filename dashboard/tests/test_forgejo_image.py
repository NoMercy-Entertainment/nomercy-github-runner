"""The properties of the Forgejo runner image and its script that the
dashboard depends on.

Not a build test - building it takes minutes and needs the network. These are
the things that, if they drift, break the deployment silently rather than
loudly: the nested daemon (prune and the disk figures read it), the storage
driver (overlay-on-overlay does not mount), the build cache cap (a runner
filled a 1 TB disk once already), the registration path providers.py reads,
the two commands the script must and must not run, and the compose service's
ability to build the image at all - nothing publishes that tag.
"""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

START = os.path.join(ROOT, "scripts", "start-forgejo.sh")
DOCKERFILE = os.path.join(ROOT, "forgejo-runner", "Dockerfile")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _code(path):
    """The script with its comments stripped.

    Several of the rulings below are about what the script must NOT run, and
    the comments recording why are the most likely place for those exact
    strings to appear. Asserting against the raw text would make the
    explanation fail the test.
    """
    return "\n".join(ln for ln in _read(path).splitlines()
                     if ln.strip() and not ln.lstrip().startswith("#"))


def test_the_runner_has_its_own_docker_daemon():
    assert "dockerd" in _read(START)


def test_the_daemon_uses_fuse_overlayfs():
    """The kernel cannot stack native overlay2 on the host's overlay."""
    assert "fuse-overlayfs" in _read(START)
    assert "fuse-overlayfs" in _read(DOCKERFILE)


def test_the_build_cache_is_capped():
    """Without a gc policy the nested daemons grow without limit. This is not
    theoretical - they once filled a 1 TB disk.

    maxUsedSpace, not just "builder" and "gc" as independent substrings: the
    deprecated `defaultKeepStorage` spelling contains both of those words and
    caps nothing at all on a current daemon, so the old assertions would have
    passed the exact configuration that filled the disk. The number is not
    asserted - 20GB vs 40GB is a tuning decision - only that a cap is named
    in the form the daemon still honours."""
    start = _read(START)
    assert "maxUsedSpace" in start, (
        "the gc policy must cap with maxUsedSpace; defaultKeepStorage is "
        "deprecated and means no cap"
    )
    assert "defaultKeepStorage" not in start


def test_registration_lands_where_providers_expects_it():
    import providers
    assert providers.FORGEJO.registration_path == "/data/.runner"
    start = _read(START)
    assert "/data" in start
    assert "forgejo-runner register" in start


def test_the_script_does_not_pretend_to_deregister():
    """forgejo-runner has no `unregister` subcommand.

    It was called here anyway, inside `timeout ... || true`, so its absence
    was completely silent: every shutdown logged "deregistering..." and
    deregistered nothing. Deregistration is the dashboard's job through
    Forgejo's admin API (docker_ops.remove()), and this container cannot do
    it even in principle - it holds a registration token, not an admin token.

    Asserting the absence, not just the current text, because the failure
    this replaces is somebody adding the call back in good faith."""
    code = _code(START)
    assert "forgejo-runner unregister" not in code
    # Nothing executable mentions it at all - not even a renamed helper.
    assert "unregister" not in code


def test_the_trap_machinery_survives_shutdown():
    """The daemon is started as a background child and waited on, not exec'd.
    A trap handler does not survive exec - the shell's process image is
    replaced. Keeping this shell alive as PID 1 is what lets SIGTERM reach
    the daemon child, which is what makes `docker stop` return promptly
    instead of waiting out the full 60s grace period. This pattern is
    documented in scripts/start.sh:447-452."""
    start = _read(START)
    # The daemon is not exec'd (which would replace the shell and lose traps)
    assert "exec forgejo-runner daemon" not in start
    # Traps exist, and they reach the child
    assert "trap" in start and "stop_runner" in start
    # The script waits on the runner child rather than exiting immediately
    assert "wait" in start and "RUNNER_PID" in start


def test_the_daemon_is_started_without_a_config_flag():
    """`forgejo-runner daemon --config <missing file>` is FATAL - "invalid
    configuration: open config file ...: no such file or directory" - while
    the same command with no --config at all starts on built-in defaults.

    `register` writes .runner and never writes a config.yaml, so pointing
    --config at one made first boot an infinite loop: register succeeds,
    daemon exits non-zero, restart: unless-stopped brings it back, .runner
    now exists so registration is skipped, daemon fails again, for ever.

    If a config file is ever wanted it must be generated first
    (`forgejo-runner generate-config`); an explicit --config must always name
    a file that exists."""
    code = _code(START)
    assert "forgejo-runner daemon &" in code
    assert "--config" not in code


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


def test_runner_pid_is_declared_before_the_traps():
    """stop_runner reads RUNNER_PID, and the real `RUNNER_PID=$!` does not run
    until after the daemon is forked, well below where the traps are
    installed. Under `set -u`, a signal landing in that window before this
    declaration existed hit "RUNNER_PID: unbound variable" — fatal and
    immediate, exiting 1 instead of 143/130 and skipping the SIGTERM to the
    child entirely (measured by direct reproduction: a harness with this
    declaration removed dies exactly that way). This is a positional check,
    not just a presence check, so a future edit that moves the traps above
    the declaration — rather than removing it outright — still fails."""
    start = _read(START)
    declare_pos = start.index('RUNNER_PID=""')
    first_trap_pos = start.index("\ntrap '")
    assert declare_pos < first_trap_pos, (
        "RUNNER_PID=\"\" must appear before the first `trap` line"
    )


COMPOSE = os.path.join(ROOT, "docker-compose.runners.yml")


def test_compose_can_build_the_forgejo_image_itself():
    """Nothing publishes ghcr.io/.../nomercy-forgejo-runner - the CI job in
    .github/workflows/build-image.yml builds the GitHub runner image only. So
    without a build stanza the documented deployment ends at `manifest
    unknown`, and so does the dashboard's "+ Add runner" for Forgejo, which
    resolves the same tag.

    The context must be the repository root: forgejo-runner/Dockerfile does
    `COPY scripts/start-forgejo.sh`, which a context rooted at the
    Dockerfile's own directory cannot see."""
    import yaml
    with open(COMPOSE, encoding="utf-8") as fh:
        svc = yaml.safe_load(fh)["services"]["forgejo-runner-1"]
    assert "build" in svc, "forgejo-runner-1 has no build stanza"
    assert svc["build"]["context"] == "."
    assert svc["build"]["dockerfile"] == "forgejo-runner/Dockerfile"
    # image: is kept alongside build: so the built image carries the tag
    # providers.FORGEJO resolves by default.
    import providers
    assert svc["image"] == providers.FORGEJO.image


def test_the_compose_forgejo_runner_is_capped_like_the_github_fleet():
    """docker_ops.create() applies RUNNER_CPU_LIMIT/RUNNER_MEM_LIMIT to every
    Forgejo runner the dashboard creates. Without the same limits here, the
    one runner declared in compose would be the single container in the fleet
    running unbounded - two paths in one branch, two policies."""
    import yaml
    with open(COMPOSE, encoding="utf-8") as fh:
        compose = yaml.safe_load(fh)
    forgejo = compose["services"]["forgejo-runner-1"]["deploy"]["resources"]
    github = compose["services"]["github-runner-1"]["deploy"]["resources"]
    assert forgejo["limits"] == github["limits"]
