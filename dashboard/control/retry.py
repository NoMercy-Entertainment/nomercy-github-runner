"""Every remote call has a deadline, and a bound on how often it is tried.

The table in design section 17.2, as data. A test parses that table out of the
spec and compares it with `POLICIES`, so the two cannot disagree - the same
arrangement the state machine has with its diagram.

**A remote collaborator is never called directly** from `dashboard/control/`.
It is handed to `call()` together with the policy that bounds it. That is a
rule a machine can check, and a test does: it walks the package's syntax trees
and fails on any direct call to an agent, a forge, a runtime or a token mint.
Without it "no call is unbounded" would be true until the first time someone
forgot, and nobody would find out until a worker stopped answering and took the
controller with it.

**The deadline is enforced from the outside.** The call runs on a worker thread
and the caller stops waiting when the deadline passes. The adapters underneath
have deadlines of their own - the Docker CLI wrapper and both forge clients set
socket timeouts - and those are what actually stop the work. This is the outer
guarantee: whatever an adapter does or forgets, the controller is released on
time. Python cannot kill a thread, so a call that outlives its deadline is
abandoned rather than stopped; its thread is a daemon and cannot keep the
process alive. That is stated rather than hidden because it is the one way this
module is weaker than its name.

**A failed status read is cached as unknown**, never as the last good answer.
Serving a stale "idle" after the forge stopped answering is precisely how a
cache clear would be allowed to act on a runner that had since picked up a job.
"""
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple


@dataclass(frozen=True)
class Policy:
    """One row of design 17.2."""

    name: str
    timeout: float
    retries: int
    backoff: Tuple[float, ...] = ()

    def __post_init__(self):
        if self.timeout <= 0:
            raise ValueError(f"{self.name}: a deadline must be positive")
        if len(self.backoff) != self.retries:
            raise ValueError(f"{self.name}: one backoff per retry")


AGENT_FAST = Policy("agent-fast", 10, 2, (1, 3))
AGENT_SLOW = Policy("agent-slow", 300, 0)
FORGE_REGISTRATION = Policy("forge-registration", 20, 2, (2, 6))
FORGE_STATUS = Policy("forge-status", 20, 0)
FORGE_DELETE = Policy("forge-delete", 20, 0)
HEARTBEAT = Policy("heartbeat", 5, 0)

#: Keyed by the "Call" column of the design's table, so the test can line each
#: row up with its policy.
POLICIES = {
    "Agent verb, fast": AGENT_FAST,
    "Agent verb, slow": AGENT_SLOW,
    "Forge registration": FORGE_REGISTRATION,
    "Forge status poll": FORGE_STATUS,
    "Forge record deletion": FORGE_DELETE,
    "Heartbeat": HEARTBEAT,
}

#: Replaced in tests, so backoff does not make the suite wait.
sleep = time.sleep


class DeadlineExceeded(TimeoutError):
    def __init__(self, policy, attempt):
        super().__init__(
            f"{policy.name}: no answer within {policy.timeout:g}s "
            f"(attempt {attempt} of {policy.retries + 1})")
        self.policy = policy


class GaveUp(Exception):
    """Every attempt the policy allows has failed. Carries the last cause and
    how many tries it took, because "failed" and "failed three times" call for
    different responses."""

    def __init__(self, policy, attempts, cause):
        super().__init__(f"{policy.name}: failed after {attempts} "
                         f"attempt{'s' if attempts != 1 else ''}: {cause}")
        self.policy = policy
        self.attempts = attempts
        self.cause = cause


def _within(deadline, fn, args, kwargs, policy, attempt):
    """Run fn, waiting no longer than the deadline for it."""
    outcome = {}

    def run():
        try:
            outcome["value"] = fn(*args, **kwargs)
        except BaseException as e:      # noqa: BLE001 - handed back below
            outcome["error"] = e

    worker = threading.Thread(target=run, daemon=True,
                              name=f"call:{policy.name}")
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        raise DeadlineExceeded(policy, attempt)
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("value")


def call(policy: Policy, fn: Callable, *args, **kwargs) -> Any:
    """Call `fn` under `policy`: a deadline on every attempt, at most
    `retries` more attempts, with the listed pauses between them.

    With no retries the original exception is raised as it was, so a caller
    that already handles a specific failure keeps handling it. With retries,
    exhausting them raises GaveUp carrying the last cause.
    """
    last = None
    for attempt in range(1, policy.retries + 2):
        try:
            return _within(policy.timeout, fn, args, kwargs, policy, attempt)
        except Exception as e:          # noqa: BLE001
            last = e
            if attempt <= policy.retries:
                sleep(policy.backoff[attempt - 1])
    if policy.retries == 0:
        raise last
    raise GaveUp(policy, policy.retries + 1, last) from last


class CachedForges:
    """A Forges collaborator whose status reads are cached, the 17.2 way.

    A successful read is kept for `ttl` seconds. A failed one is cached as
    None - unknown - for the same ttl, and the previous good answer is thrown
    away rather than served. Holding on to it would report a runner as idle
    after the forge had stopped being able to say so.

    Deletion is passed straight through: a delete is never answered from a
    cache.
    """

    def __init__(self, forges, ttl=10.0,
                 clock: Optional[Callable[[], float]] = None):
        self.forges = forges
        self.ttl = ttl
        self.clock = clock or time.monotonic
        self._cache = {}

    def records(self, provider):
        key = getattr(provider, "key", provider)
        hit = self._cache.get(key)
        now = self.clock()
        if hit and now - hit[0] < self.ttl:
            return hit[1]
        try:
            value = call(FORGE_STATUS, self.forges.records, provider)
        except Exception:               # noqa: BLE001
            value = None
        self._cache[key] = (now, value)
        return value

    def delete(self, provider, registration_id):
        return call(FORGE_DELETE, self.forges.delete, provider,
                    registration_id)
