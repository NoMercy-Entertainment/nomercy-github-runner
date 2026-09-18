"""The agent and the forges, as stand-ins that remember what they were told.

Both are collaborators of the provisioning flow that are the same for every
cell - which is exactly why they can be faked once here and used for all six.
Phase 3 builds the real agent; phases 8 and 9 talk to the real forges.
"""


class FakeAgent:
    def __init__(self, ready=True):
        self.calls = []
        self._ready = ready
        self.fail_on = set()
        self.raise_with = None      # text for the exception, to test redaction
        self.ready_after = 0        # number of `ready` calls that say no first

    def _maybe_fail(self, name):
        if name in self.fail_on:
            raise RuntimeError(self.raise_with or f"agent {name} failed")

    def register(self, host_id, ref, plan):
        # The plan is inspected here only to prove it arrived; its token is
        # never stored, only compared.
        self.calls.append(("register", host_id, ref.handle,
                           bool(plan and plan.token)))
        self._maybe_fail("register")
        return {"registration_id": f"77{ref.handle[-4:]}",
                "registration_uuid": f"uuid-{ref.handle}"}

    def deregister(self, host_id, ref):
        self.calls.append(("deregister", host_id, ref.handle))
        self._maybe_fail("deregister")

    def drain(self, host_id, ref):
        self.calls.append(("drain", host_id, ref.handle))

    def cancel_drain(self, host_id, ref):
        self.calls.append(("cancel_drain", host_id, ref.handle))

    def ready(self, host_id, ref):
        self.calls.append(("ready", host_id, ref.handle))
        if self.ready_after > 0:
            self.ready_after -= 1
            return False
        return self._ready


class FakeForges:
    """Answers as both forges would, for whatever the agent registered.

    `online` controls whether a registered runner shows as online; `busy`
    names registration ids the forge reports as running a job.
    """

    def __init__(self, agent, online=True):
        self.agent = agent
        self.online = online
        self.busy = set()
        self.idle_overrides = set()
        self.deleted = []
        self.delete_ok = True

    def records(self, provider):
        out = []
        for call in self.agent.calls:
            if call[0] != "register":
                continue
            handle = call[2]
            rid = f"77{handle[-4:]}"
            status = "online" if self.online else "offline"
            out.append({"id": rid, "status": status, "busy": rid in self.busy,
                        "uuid": f"uuid-{handle}",
                        # Forgejo's words
                        **({"status": ("active" if rid in self.busy
                                       else "idle" if self.online
                                       else "offline")}
                           if provider.key == "forgejo" else {})})
        return out

    def delete(self, provider, registration_id):
        self.deleted.append((provider.key, registration_id))
        return self.delete_ok
