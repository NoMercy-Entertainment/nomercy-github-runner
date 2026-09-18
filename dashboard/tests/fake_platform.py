"""The agent and the forges, as stand-ins that remember what they were told.

Both are collaborators of the provisioning flow that are the same for every
cell - which is exactly why they can be faked once here and used for all six.
Phase 3 builds the real agent; phases 8 and 9 talk to the real forges.
"""


class Crash(BaseException):
    """The controller process dying, not a step failing.

    A BaseException so it passes through every `except Exception` in the flow
    and the reconciler exactly as a real crash would: no compensation runs, no
    state is written, the pass simply stops where it was.
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

    #: Raise Crash on the next `ready` - "register before confirm".
    crash_on_ready = False

    def ready(self, host_id, ref):
        self.calls.append(("ready", host_id, ref.handle))
        if self.crash_on_ready:
            self.crash_on_ready = False
            raise Crash("the controller died waiting for the runner")
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

    def live_handles(self):
        """Units with a forge record right now: registered, and not since
        deregistered by the agent or deleted through the API."""
        live = []
        for call in self.agent.calls:
            handle = call[2] if len(call) > 2 else None
            if call[0] == "register" and handle not in live:
                live.append(handle)
            elif call[0] == "deregister" and handle in live:
                live.remove(handle)
        gone = {rid for _, rid in self.deleted}
        return [h for h in live if f"77{h[-4:]}" not in gone]

    def records(self, provider):
        out = []
        for handle in self.live_handles():
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
