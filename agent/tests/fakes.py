"""A runtime and a registrar that remember what they were asked.

In phase 4 the real runtimes arrive; these stand in for them so the verb table
can be tested on its own. `calls` records every call with its arguments, which
is how the tests show that what reached the runtime is the validated value and
not the raw request.
"""


class FakeRuntime:
    def __init__(self, raise_with=None, units=None):
        self.calls = []
        self.raise_with = raise_with
        #: What `instances()` reports: [{"runner_id", "state"}, ...], or an
        #: exception to raise, for the case where the runtime cannot tell.
        self.units = units if units is not None else []

    def _record(self, *call):
        self.calls.append(call)
        if self.raise_with:
            raise RuntimeError(self.raise_with)

    def create(self, runner_id, spec):
        self._record("create", runner_id, spec)
        return f"rnr-{runner_id}"

    def start(self, runner_id):
        self._record("start", runner_id)

    def stop(self, runner_id):
        self._record("stop", runner_id)

    def restart(self, runner_id):
        self._record("restart", runner_id)

    def remove(self, runner_id, keep_data):
        self._record("remove", runner_id, keep_data)

    def status(self, runner_id):
        self._record("status", runner_id)
        return {"exists": True, "running": True}

    def telemetry(self, runner_id):
        self._record("telemetry", runner_id)
        return {"cpu_percent": 1.5}

    def logs(self, runner_id, since_seconds):
        self._record("logs", runner_id, since_seconds)
        return "a line"

    def probe(self, runner_id, probe):
        self._record("probe", runner_id, probe)
        return {"ok": True, "value": "1GB"}

    def clear_cache(self, runner_id, policy):
        self._record("clear_cache", runner_id, policy)
        return {"total_bytes": 10}

    def capabilities(self):
        return {"job_containers": True}

    def instances(self):
        if isinstance(self.units, Exception):
            raise self.units
        return list(self.units)


class FakeRegistrar:
    def __init__(self):
        self.calls = []

    def register(self, runner_id, plan):
        self.calls.append(("register", runner_id, dict(plan)))
        return {"registration_id": 42, "registration_uuid": "u-1"}

    def deregister(self, runner_id):
        self.calls.append(("deregister", runner_id))
