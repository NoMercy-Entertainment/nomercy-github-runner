"""An executor that does nothing in the world and remembers what it was asked.

The reconciler decides and the executor acts. Separating them is what lets the
deciding - the part that has to be right about never aborting a job and never
overshooting a fleet - be tested exhaustively without a worker, a forge or a
container engine.

`MUTATING` names the calls that would change something real. A converged fleet
must produce none of them, and that is the test for convergence: a pass that
only looks is a pass that did no work.
"""

MUTATING = frozenset({"provision", "register", "start", "stop", "drain",
                      "cancel_drain", "deregister", "remove", "clear_cache",
                      "abandon"})


class FakeExecutor:
    def __init__(self, host_id="linux-worker-1", fail_on=()):
        self.host_id = host_id
        self.fail_on = set(fail_on)
        self.calls = []
        #: runner_id -> the state `observe` should report. Tests set this to
        #: simulate a job starting or finishing.
        self.world = {}

    def _record(self, name, spec, **extra):
        self.calls.append((name, spec["runner_id"], extra))
        if name in self.fail_on:
            raise RuntimeError(f"{name} failed on purpose")

    def mutations(self):
        return [c for c in self.calls if c[0] in MUTATING]

    # ---- the executor protocol --------------------------------------------

    def observe(self, spec):
        self.calls.append(("observe", spec["runner_id"], {}))
        return self.world.get(spec["runner_id"])

    def provision(self, spec, on_placed=None):
        self._record("provision", spec)
        if on_placed:
            on_placed(self.host_id)
        return {"exec_unit_ref": f"unit-{spec['runner_id'][:8]}",
                "host_id": self.host_id}

    def register(self, spec, on_registered=None):
        self._record("register", spec)
        ids = {"registration_id": f"reg-{spec['runner_id'][:8]}",
               "registration_uuid": None}
        if on_registered:
            on_registered(ids)
        return ids

    def abandon(self, spec):
        self._record("abandon", spec)
        return ("deregister", "remove_unit")

    def start(self, spec):
        self._record("start", spec)

    def stop(self, spec):
        self._record("stop", spec)

    def drain(self, spec):
        """Drained at once unless the world says the runner has a job - the
        real flow asks the forge the same question."""
        self._record("drain", spec)
        return self.world.get(spec["runner_id"]) not in ("busy", "draining")

    def cancel_drain(self, spec):
        self._record("cancel_drain", spec)

    def deregister(self, spec):
        self._record("deregister", spec)

    def remove(self, spec, keep_data=False):
        self._record("remove", spec, keep_data=keep_data)

    def clear_cache(self, spec):
        self._record("clear_cache", spec)
        return {"total_bytes": 1024}
