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
        #: Units told to drain on their worker and not since told otherwise.
        #: Their runner exits once it has no job; FakeForges says whether it
        #: has one, and sets itself here when it is built.
        self.drained = set()
        self.forges = None
        #: Set to make `running` fail, as an agent that cannot answer does.
        self.running_unknown = False
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
        # Recorded only once it has happened: the forge's view is built from
        # these calls, and a deregistration that failed left the record there.
        self._maybe_fail("deregister")
        self.calls.append(("deregister", host_id, ref.handle))

    def drain(self, host_id, ref):
        self.calls.append(("drain", host_id, ref.handle))
        self._maybe_fail("drain")
        self.drained.add(ref.handle)

    def cancel_drain(self, host_id, ref):
        self.calls.append(("cancel_drain", host_id, ref.handle))
        self._maybe_fail("cancel_drain")
        self.drained.discard(ref.handle)

    def exited(self, handle):
        """A unit drained on its worker whose runner has finished its job,
        and so exited."""
        return handle in self.drained and not (
            self.forges and self.forges.working(handle))

    def running(self, host_id, ref):
        self.calls.append(("running", host_id, ref.handle))
        if self.running_unknown:
            raise RuntimeError("the agent did not answer")
        return not self.exited(ref.handle)

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
        agent.forges = self
        self.online = online
        #: Registration ids drained at the forge - taken out of job matching
        #: - and whether the forge confirms that when asked.
        self.drained = set()
        self.drain_ok = True
        self.busy = set()
        self.idle_overrides = set()
        self.deleted = []
        self.delete_ok = True
        #: (registration id, how many agent calls had happened) per deletion,
        #: so a deletion ends the record that existed then and not one a
        #: later registration of the same unit makes - as a real forge would.
        self._deleted_at = []

    def forget(self, registration_id):
        """The forge loses a record by itself - deleted by hand, say."""
        self._deleted_at.append((registration_id, len(self.agent.calls)))

    def live_handles(self):
        """Units with a forge record right now: registered, and not since
        deregistered by the agent or deleted through the API."""
        live = []
        pending = sorted(self._deleted_at, key=lambda d: d[1])

        def apply(upto):
            while pending and pending[0][1] <= upto:
                rid, _ = pending.pop(0)
                for h in list(live):
                    if f"77{h[-4:]}" == rid:
                        live.remove(h)
        for i, call in enumerate(self.agent.calls):
            apply(i)
            handle = call[2] if len(call) > 2 else None
            if call[0] == "register" and handle not in live:
                live.append(handle)
            elif call[0] == "deregister" and handle in live:
                live.remove(handle)
        apply(len(self.agent.calls))
        return live

    def working(self, handle):
        return f"77{handle[-4:]}" in self.busy

    def drain(self, provider, spec):
        if not self.drain_ok:
            raise RuntimeError("the forge did not confirm the drain")
        self.drained.add(str(spec.get("registration_id")))

    def cancel_drain(self, provider, spec):
        if not self.drain_ok:
            raise RuntimeError("the forge did not confirm the runner back")
        self.drained.discard(str(spec.get("registration_id")))

    def records(self, provider):
        out = []
        for handle in self.live_handles():
            rid = f"77{handle[-4:]}"
            # A runner drained on its worker exits once its job is done, and
            # its forge then sees it go offline.
            up = self.online and not self.agent.exited(handle)
            status = "online" if up else "offline"
            out.append({"id": rid, "status": status, "busy": rid in self.busy,
                        "uuid": f"uuid-{handle}",
                        # Forgejo's words
                        **({"status": ("active" if rid in self.busy
                                       else "idle" if up
                                       else "offline")}
                           if provider.key == "forgejo" else {})})
        return out

    def delete(self, provider, registration_id):
        # Recorded only when it happened: a delete the forge refused leaves
        # the record where it was, and the forge's view must say so.
        self.delete_attempts = getattr(self, "delete_attempts", []) + [
            (provider.key, registration_id)]
        if self.delete_ok:
            self.deleted.append((provider.key, registration_id))
            self._deleted_at.append((registration_id, len(self.agent.calls)))
        return self.delete_ok
