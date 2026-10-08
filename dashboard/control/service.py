"""The one service every runner action goes through.

**It never performs work.** A call validates, writes what should be true, and
returns an operation id. Nothing here talks to a forge, an agent or an engine.
That is not a stylistic preference: the shape it replaces is a route handler
that shelled out to Docker and returned when it was done, which could not be
retried, could not be watched, and left half a runner behind whenever the
request timed out first.

**Impossible is refused here, before anything exists.** A cell no provider
supports, a cell with no runtime to execute it, a verb the runner's state does
not allow - each is refused at the moment it is asked for, with a reason. The
alternative is discovering it four steps later, on a runner that has already
been half created and now has to be cleaned up.

**Which runtime runs a cell is a table.** `RUNTIMES` below is data, so adding
the Windows worker is a row rather than a branch, and a test proves the lookup
is data-driven by replacing the table. The moment this becomes an `if platform
== "windows"` the design has lost the property it exists for.
"""
import importlib
import re
from decimal import Decimal

import providers
from store.fleets import FleetStore
from store.specs import SpecStore

from . import cpusets, hardware, placement, retry, states
from .inventory import Inventory
from .operations import OperationStore

#: Which kind of execution unit a platform runs in. A table rather than a
#: conditional for the same reason RUNTIMES is one. Strings, resolved in
#: `_runtime_and_ref()`, so the service does not load the runtime package
#: just to plan.
EXEC_KINDS = {
    providers.LINUX: "linux-container",
    providers.WINDOWS: "windows-process",
    providers.MACOS: "macos-appliance",
}

COMBINED_MEMORY_PLATFORMS = frozenset({providers.LINUX})

#: Where a whole-number CPU limit pins each runner to its own window of cores.
#: On Linux a quota leaves `nproc` at the host's count, and builds size
#: themselves by it (control/cpusets.py); on Windows a Job Object's CPU rate
#: leaves a build's own processor count at the host's the same way, and only
#: an affinity mask changes it (agent/jobhost.py: `cpu_rate`, `affinity_mask`,
#: 2026-09-23). macOS pins through its appliance's own CPU enforcement
#: instead (`placement.enforces_appliance_limits`), so it stays out of here.
PINNED_CPU_PLATFORMS = frozenset({providers.LINUX, providers.WINDOWS})

#: (provider, platform) -> the runtime that executes that cell, as
#: "module:attribute". Strings rather than imports so this stays a table of
#: data: nothing here is loaded until a cell is actually used, and a test can
#: replace the whole table to prove the lookup is not a hidden conditional.
#:
#: Empty by default. Nothing executes a runner in this process any more: every
#: cell's units live on a worker and are reached through its agent, so both
#: callers that plan anything - the controller (control/main.py) and the
#: dashboard's v2 API (api_v2.py) - pass `control.agent_runtime.TABLE`. A
#: default naming a local runtime would be a second way to execute a runner
#: that only ever fires where someone forgot to pass one.
RUNTIMES = {}

#: What a verb means for a runner's desired state. Absent from this table means
#: the verb is an operation that does not change what the runner should be -
#: clearing a cache does not make a running runner any less meant to run.
DESIRED_BY_VERB = {
    "start": "running",
    "stop": "stopped",
    "restart": "running",
    "drain": "drained",
    "cancel_drain": "running",
    "remove": "absent",
    "recreate": "running",
    "repair": "running",
    "provision": "running",
    "register": "running",
    "deregister": "absent",
}

DESIRED_STATES = frozenset({"running", "stopped", "drained", "absent"})


class Refused(Exception):
    """The request cannot be carried out, and this is why.

    One exception type with a reason, rather than a bare False, for the same
    reason `Support` carries one: a refusal an operator cannot act on is a
    failure they will report as a bug.
    """


class UnknownRunner(Exception):
    pass


def forget_adoption(specs, spec):
    """End an adoption whose unit has been removed.

    The one definition of what ending an adoption means, for both the
    reconciler, when the unit has been removed, and the provisioning flow,
    when the worker reports there is none left.

    An adopted runner is driven through the unit that was already there.
    Once that unit is gone, keeping the record would make the next create
    try to adopt a unit it has just removed - a failure with no way out but
    an edit of the database. Its name goes with it, so the rebuilt runner
    takes the one its fleet gives (2026-09-20)."""
    if not spec.get("adopt_unit"):
        return
    fresh = specs.get(spec["runner_id"])
    name = None
    try:
        name = RunnerService(specs.path).next_name(fresh["fleet_id"])
    except Exception:                           # noqa: BLE001
        pass
    specs.update(fresh["runner_id"], fresh["spec_version"], adopt_unit=None,
                 **({"display_name": name} if name else {}))


class RunnerService:
    def __init__(self, path=None, specs=None, fleets=None, operations=None,
                 inventory=None, runtimes=None, agents=None, env=None):
        self.specs = specs or SpecStore(path)
        self.fleets = fleets or FleetStore(path)
        self.operations = operations or OperationStore(path)
        self.inventory = inventory or Inventory(path)
        #: Injectable so a test can prove the lookup is data-driven.
        self.runtimes = RUNTIMES if runtimes is None else runtimes
        #: How workers are reached (control/agent_runtime.AgentWiring), set by
        #: the controller process. None where nothing runs on a worker.
        self.agents = agents
        #: The deployment's settings, as the controller process was given
        #: them. A cell can exist only because of one - Forgejo on Windows
        #: exists when FORGEJO_RUNNER_ARTIFACT_WINDOWS names a self-built
        #: runner - so a plan that asked without them refused a fleet the
        #: store itself records as available.
        self.env = dict(env or {})

    # ---- which runtime runs a cell -----------------------------------------

    def runtime_for(self, provider_key, platform):
        """The runtime class for a cell, or a refusal naming the gap.

        Resolved by table lookup and imported on use. A cell with no entry is
        not an error in the abstract - it is a platform this build cannot
        execute yet, and saying so is more useful than a KeyError.
        """
        target = self.runtimes.get((provider_key, platform))
        if not target:
            raise Refused(
                f"no runtime is registered for {provider_key}/{platform}; "
                f"this build can execute "
                f"{sorted({p for _, p in self.runtimes})}")
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        return getattr(module, attribute)

    def runtime(self, spec):
        """The runtime adapter for one runner. One whose units live on a
        worker is bound to that runner's worker (`for_runner`); one that runs
        where the controller runs is simply made."""
        factory = self.runtime_for(spec["provider"], spec["platform"])
        bind = getattr(factory, "for_runner", None)
        return bind(self, spec) if bind is not None else factory()

    def can_execute(self, provider_key, platform):
        return (provider_key, platform) in self.runtimes

    # ---- planning ----------------------------------------------------------

    def unit_image(self, fleet):
        """What a unit of this fleet is made from, the way the runtime will
        resolve it: what the deployment names for the cell, else the fleet's
        own template. A reference may carry its digest after a space - the
        worker knows it by its name."""
        from .main import unit_images
        arch_key = ("RUNNER_UNIT_IMAGE_" + fleet["provider"].upper() +
                    "_" + fleet["platform"].upper() + "_" +
                    fleet["architecture"].upper())
        named = unit_images(self.env or {}).get(
            (fleet["provider"], fleet["platform"]))
        arch_named = (self.env or {}).get(arch_key)
        return str(fleet.get("unit_template") or arch_named or named or fleet.get("template") or "").split(" ")[0]

    def effective_spec(self, spec, env=None, host_id=None):
        """Resolve deployment defaults before either scheduling or creation.

        `host_id`: this runner's host, once placement has chosen one - the
        only time a pinned platform's `cpu_limit` is actually computed here.
        Left out (the default), a pinned fleet's number is never turned into
        a window: it is a width, and a width is not a window until there is
        a host to cut it from. That is deliberate, not an oversight - a
        window computed before a host is known is exactly how a runner
        ended up pinned to cores that belonged to a different machine
        (finding 1, 2026-09-23), and every caller that does not yet have a
        real host (`plan()`, a placement fit-check, a reservation computed
        for somebody else's placement) must leave it unset rather than
        guess. The one caller that does have one (`ProvisioningFlow.
        _step_create_unit`, once its own placement step succeeds) passes it
        explicitly."""
        result = dict(spec)
        fleet = self.fleets.get(spec.get("fleet_id")) or {}
        cell = (spec.get("provider"), spec.get("platform"))
        for field in ("cpu_limit", "memory_limit", "memory_swap_limit", "disk_limit"):
            if result.get(field) is None and fleet.get(field) is not None:
                if field == "cpu_limit" and self._pinned_width(fleet) is not None:
                    if host_id is None:
                        # A pinned fleet's number is a window width, which
                        # only a known host turns into cores; read as a
                        # quota it would bound nothing `nproc` reports.
                        continue
                    result["cpu_limit"] = self._cpu_window(
                        fleet["platform"], self._pinned_width(fleet),
                        host_id=host_id,
                        exclude={result["runner_id"]} if result.get("runner_id") else ())
                    continue
                result[field] = fleet[field]
        if result.get("memory_limit") is None:
            from .main import unit_memory
            defaults = getattr(self.agents, "memory", {}) or unit_memory(self.env if env is None else env)
            source_env = self.env if env is None else env
            arch_key = ("RUNNER_UNIT_MEMORY_" + result["provider"].upper() +
                        "_" + result["platform"].upper() + "_" +
                        result["architecture"].upper())
            value = (source_env or {}).get(arch_key) or defaults.get(cell)
            if value:
                match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?)(?:i?b)?",
                                     str(value).strip(), re.IGNORECASE)
                if not match:
                    raise Refused(f"invalid memory limit {value!r} for {cell}")
                result["memory_limit"] = int(Decimal(match[1]) *
                    (1024 ** ("kmgtpe".index(match[2].lower()) + 1)
                     if match[2] else 1))
                if result["memory_limit"] <= 0:
                    raise Refused("memory limit must be positive")
        if not result.get("runtime_template") or result.get("runtime_template") == fleet.get("template"):
            result["runtime_template"] = self.unit_image(fleet or result)
        for field in ("memory_limit", "memory_swap_limit", "disk_limit"):
            if result.get(field) is not None and int(result[field]) <= 0:
                raise Refused(f"{field} must be positive when configured")
        if result.get("memory_swap_limit") is not None:
            if result.get("platform") not in COMBINED_MEMORY_PLATFORMS:
                raise Refused("combined RAM and swap limits are Linux-only")
            if not result.get("memory_limit") or int(result["memory_swap_limit"]) < int(result["memory_limit"]):
                raise Refused("combined RAM and swap limit must be at least the RAM limit")
        return result

    def replacement_spec(self, spec):
        """A replacement takes current fleet defaults, preserving its identity."""
        fleet = self.fleets.get(spec.get("fleet_id"))
        if not fleet:
            raise Refused("recreate requires an existing fleet")
        result = dict(spec, runtime_template=self.unit_image(fleet),
                      labels=fleet.get("labels") or [],
                      cache_policy=fleet.get("cache_policy"),
                      runner_group=fleet.get("runner_group"))
        for field in ("cpu_limit", "memory_limit", "memory_swap_limit", "disk_limit"):
            result[field] = fleet.get(field) if fleet.get(field) is not None else spec.get(field)
        width = self._pinned_width(fleet)
        if width is not None:
            own = spec.get("cpu_limit")
            if cpusets.is_cpuset(own) and len(cpusets.parse(own)) == width:
                # Its window is still the right size: moving it would change
                # nothing but which cores it shares, and churn the others.
                result["cpu_limit"] = own
            else:
                result["cpu_limit"] = self._cpu_window(
                    fleet["platform"], width, exclude={spec["runner_id"]},
                    host_id=spec.get("host_id"))
        return self.effective_spec(result)

    # ---- pinned CPU windows (Linux, Windows) -------------------------------

    def _pinned_width(self, fleet):
        """How many cores each runner of this fleet is pinned to, or None when
        the fleet asks for no pinning. Only the platforms in
        `PINNED_CPU_PLATFORMS` pin: there a quota leaves a build's own
        processor count at the host's, and builds size themselves by it."""
        if (fleet or {}).get("platform") not in PINNED_CPU_PLATFORMS:
            return None
        return cpusets.whole_cores(fleet.get("cpu_limit"))

    def _any_host_could_hold(self, platform, width):
        """Whether some healthy worker of `platform`, known today by its own
        declaration or by a runner's last telemetry, could hold a window
        this wide at all - not yet asking which one, only whether the
        fleet's own setting is achievable anywhere.

        This is the existence check `plan()` makes before any spec exists
        and before any host is chosen (`max` across hosts): a width the
        smallest host cannot hold may still be exactly what the largest one
        is for, so refusing on the pooled minimum, the way this code used
        to, would refuse plans that a real placement could have served.
        `_host_cores` asks the opposite, narrower question - what one
        already-chosen host actually has - once placement has chosen one
        (finding 1, 2026-09-23).
        """
        kind = placement.WORKER_KIND[platform]
        per_host = {}
        for worker in self.inventory.healthy(kind=kind):
            value = placement.declared(worker, "host_cores")
            if isinstance(value, int) and value > 0:
                per_host[worker["host_id"]] = value
        for spec in self.specs.list():
            if spec.get("platform") != platform:
                continue
            host_id = spec.get("host_id")
            if not host_id or host_id in per_host:
                continue
            value = (spec.get("telemetry") or {}).get("host_cores")
            if isinstance(value, int) and value > 0:
                per_host[host_id] = value
        return any(cores >= width for cores in per_host.values())

    def _host_cores(self, platform, host_id):
        """The core count `host_id`'s window is cut from: its own
        declaration, else what its own placed runners last measured, else a
        refusal naming it.

        Scoped to exactly the host asked about, never pooled across two: a
        small guest and the physical host behind it are different machines
        with their own, independent processor numbering, so mixing their
        counts is wrong in both directions - it can cut the big host's
        windows down to the small guest's size, or hand the small guest a
        window off the big host's numbering, which is how a runner on an
        eight-processor guest was pinned to cores 32-47 (2026-09-23).
        `host_id` is required on purpose: a window is only ever real once a
        host is known (`effective_spec`'s own `host_id` parameter), and a
        caller with no host yet has `_any_host_could_hold` to ask instead,
        not a pooled number pretending to answer for a host that has not
        been chosen.
        """
        cpus = hardware.of_host(self.inventory, self.specs, platform, host_id)["cpus"]
        if cpus is None:
            raise Refused(f"cannot pin a CPU window: host {host_id!r} has "
                          f"not said how many cores it has yet")
        return cpus

    def _cpu_window(self, platform, width, host_id, exclude=()):
        """A window of `width` cores on `host_id`'s own numbering that
        overlaps the ones in use least.

        Only specs of this same platform, on this same host, count as
        occupied - a Linux fleet's windows are cores on a Linux host and say
        nothing about what is free on a Windows one, and a window on one
        Windows host says nothing about another Windows host's own,
        independently numbered cores (2026-09-23). `host_id` is required for
        the same reason `_host_cores` requires it: there is no such thing as
        a window that is not cut from one specific host's cores.
        """
        taken = []
        for spec in self.specs.list():
            if (spec.get("platform") != platform
                    or spec.get("host_id") != host_id
                    or spec["runner_id"] in exclude
                    or spec.get("actual_state") == states.TERMINAL
                    or spec.get("deleted_at")):
                continue
            if cpusets.is_cpuset(spec.get("cpu_limit")):
                taken.append(cpusets.parse(spec["cpu_limit"]))
        try:
            return cpusets.allocate(
                width, self._host_cores(platform, host_id), taken)
        except ValueError as e:
            raise Refused(str(e)) from e

    def reserved_spec(self, spec):
        """An existing unit reserves at least its last measured memory cap."""
        result = self.effective_spec(spec)
        measured = (spec.get("telemetry") or {}).get("mem_limit_bytes")
        if measured and int(measured) > int(result.get("memory_limit") or 0):
            result["memory_limit"] = int(measured)
        measured_swap = (spec.get("telemetry") or {}).get("mem_swap_limit_bytes")
        if measured_swap and int(measured_swap) > int(result.get("memory_swap_limit") or 0):
            result["memory_swap_limit"] = int(measured_swap)
        return result

    def validate_replacement(self, spec):
        replacement = self.replacement_spec(spec)
        fleet = self.fleets.get(spec["fleet_id"])
        if not fleet["available"]:
            raise Refused(f"recreate cannot build a replacement: {fleet.get('unavailable_reason') or 'fleet is unavailable'}")
        self.runtime_for(spec["provider"], spec["platform"])
        can, why = self.buildable(spec["fleet_id"])
        if not can:
            raise Refused(f"recreate cannot build a replacement: {why}")
        workers = self.inventory.healthy(kind=placement.WORKER_KIND[spec["platform"]])
        if spec.get("host_id"):
            workers = [w for w in workers if w["host_id"] == spec["host_id"]]
        hosts = {w["host_id"] for w in workers}
        placed = [self.reserved_spec(s) for s in self.specs.list()
                  if s.get("host_id") in hosts and s["actual_state"] != "absent"]
        host, why = placement.choose(replacement, workers, placed)
        if host is None:
            raise Refused(f"recreate cannot build a replacement: {why}")
        return replacement

    def buildable(self, fid):
        """Whether some healthy worker could actually build a runner of this
        fleet, and why not when none could.

        A forge supporting a platform says nothing about this deployment. A
        worker that makes units from templates on its own disk can only make
        the ones it has; one that makes them from images can make anything
        it can pull. Asked here rather than found out on the worker, because
        a page that offers `+ Add runner` for a cell whose creation can only
        fail is a page that lies (2026-09-20).
        """
        fleet = self.fleets.get(fid)
        if fleet is None:
            return False, f"no fleet {fid}"
        kind = placement.WORKER_KIND.get(fleet["platform"])
        drives = placement.RUNTIME_KIND.get(fleet["platform"])
        workers = [w for w in self.inventory.healthy(kind=kind)
                   if (placement.declared(w, "kind") or drives) == drives]
        if not workers:
            # Not a refusal: a worker that is down comes back, and a runner
            # planned meanwhile waits in `planned` until one does. Saying so
            # is still worth it - the page shows why nothing is happening.
            return True, (f"no healthy worker that drives {drives} right "
                          f"now; a runner would wait for one")
        template = self.unit_image(fleet)
        if not template and any(
                placement.declared(w, "builds_from") == "template"
                for w in workers):
            # Nothing to look for: the fleet's own reason says what is
            # missing, and a sentence with a hole where a name should be
            # says nothing (2026-09-21).
            return False, ("this fleet names no template to build a runner "
                           "from yet")
        for worker in workers:
            if placement.declared(worker, "builds_from") != "template":
                return True, None
            if template in (placement.declared(worker, "templates") or []):
                return True, None
        # Read on a fleet's card, so it is a sentence: a Python list of
        # templates - or worse, an empty one printed as "[]" - is not an
        # answer to why the cell cannot be built (2026-09-21).
        held = sorted({t for w in workers
                       for t in (placement.declared(w, "templates") or [])})
        return False, (f"no worker has the template {template} this fleet "
                       f"is made from; the ones that could hold it have "
                       + (", ".join(held) if held else "none installed"))

    def plan(self, fid, count, requested_by=None, idempotency_key=None,
             env=None):
        """Produce `count` RunnerSpecs for a fleet, or refuse saying why.

        Validation happens before the first row is written, so a fleet that
        cannot be built produces no specs at all rather than some. Creating
        five and failing on the sixth would leave five runners the reconciler
        would dutifully build for a cell nobody can serve.

        Specs are born `planned`. Nothing has been created anywhere; the
        reconciler takes them from here.
        """
        if count < 1:
            raise ValueError("plan needs a positive count")

        fleet = self.fleets.get(fid)
        if fleet is None:
            raise Refused(f"no fleet {fid}")
        if not fleet["available"]:
            raise Refused(
                f"{fid} is unavailable: "
                f"{fleet['unavailable_reason'] or 'the cell does not exist'}")

        # The deployment's settings when the caller named none; a route that
        # passes its own still wins.
        env = self.env if env is None else env
        provider = providers.by_key(fleet["provider"])
        if provider is None:
            raise Refused(f"{fid} names an unknown provider "
                          f"{fleet['provider']!r}")

        # Asked again here rather than trusting the stored flag: seeding may
        # have run before someone built the artefact, or long before now.
        support = provider.supports(fleet["platform"], fleet["architecture"],
                                    env)
        if not support:
            raise Refused(f"{fid} cannot be built: {support.reason}")

        # Refused before anything is created, which is the whole point of
        # doing it at plan time.
        self.runtime_for(fleet["provider"], fleet["platform"])
        can, why = self.buildable(fid)
        if not can:
            raise Refused(f"{fid} cannot be built: {why}")

        # An impossible width is refused here, before a single spec exists -
        # an operator should learn a fleet's pinned width fits no worker of
        # its platform when they set it, not from a runner that fails after
        # being placed. This is an existence check across every host
        # ("could ANY of them hold it"), never `_host_cores`'s pooled
        # `min()`: which *specific* host this runner lands on is not yet
        # known, and a width the smallest host cannot hold may still be
        # exactly what the largest one is for (finding 1, 2026-09-23) - the
        # window itself is cut once placement actually chooses one.
        width = self._pinned_width(fleet)
        if width is not None and not self._any_host_could_hold(fleet["platform"], width):
            raise Refused(
                f"{fid} pins a {width}-core window, but no healthy "
                f"{fleet['platform']} worker is known to have that many "
                f"cores yet")

        # `cpu_limit=None` is forced here even though `fleet` itself carries
        # one: `fleet["cpu_limit"]` for a pinned platform is a width, not a
        # window, and starting `effective_spec` with it already "set" would
        # skip the very check that keeps a width from leaking out as if it
        # were one (finding 1, 2026-09-23).
        defaults = self.effective_spec(
            dict(fleet, runtime_template=self.unit_image(fleet), cpu_limit=None),
            env=env)
        operation, created = self.operations.open(
            "plan", fleet_id=fid, requested_by=requested_by,
            idempotency_key=idempotency_key,
            note=f"plan {count} for {fid}")
        if not created:
            return operation["operation_id"]

        runner_ids = []
        for n in range(count):
            runner_ids.append(self.specs.create(
                display_name=self.next_name(fid),
                provider=fleet["provider"],
                platform=fleet["platform"],
                architecture=fleet["architecture"],
                runtime_template=defaults.get("runtime_template"),
                # Never a window here (finding 1, 2026-09-23): a pinned
                # platform's `cpu_limit` stays unset until placement knows
                # which host to cut it from
                # (`effective_spec`'s own `host_id`, `ProvisioningFlow.
                # _step_create_unit`). `defaults.get("cpu_limit")` is None
                # for a pinned fleet for exactly that reason, and is
                # whatever quota a non-pinned one asks for otherwise.
                cpu_limit=defaults.get("cpu_limit"),
                memory_limit=defaults.get("memory_limit"),
                memory_swap_limit=defaults.get("memory_swap_limit"),
                disk_limit=defaults.get("disk_limit"),
                labels=fleet.get("labels") or [],
                runner_group=fleet.get("runner_group"),
                cache_policy=fleet.get("cache_policy"),
                fleet_id=fid,
                desired_state="running",
                actual_state="planned",
            ))

        # Planning is finished when the rows exist; there is no remote work in
        # it. Saying so keeps "pending" meaning something that is still going.
        self.operations.succeed(operation["operation_id"],
                                {"runner_ids": runner_ids})
        return operation["operation_id"]

    def next_name(self, fid):
        """What the next runner of this fleet is called: its fleet and the
        lowest free number in it.

        A name is presentation (11.2) - identity is the runner_id - and this
        is the presentation rule. The fleet used to hold three eras of
        naming at once, one per way a runner had arrived, which is no way to
        read a page (2026-09-20). A number freed by a removal is used again,
        so the set stays small and dense.
        """
        taken = set()
        for spec in self.specs.list(fleet_id=fid):
            if spec["actual_state"] == states.TERMINAL:
                continue
            name = str(spec.get("display_name") or "")
            if name.startswith(f"{fid}-") and name[len(fid) + 1:].isdigit():
                taken.add(int(name[len(fid) + 1:]))
        n = 1
        while n in taken:
            n += 1
        return f"{fid}-{n}"

    def planned_ids(self, operation_id):
        """The runner ids a `plan` produced, read back from its result."""
        import json
        operation = self.operations.get(operation_id)
        if not operation or not operation["result"]:
            return []
        return json.loads(operation["result"]).get("runner_ids", [])

    # ---- intent ------------------------------------------------------------

    def set_desired(self, runner_id, state, requested_by=None,
                    idempotency_key=None):
        """Record what a runner should be. Returns an operation id.

        Writes only `desired_state`. `actual_state` belongs to the reconciler,
        which is the only thing that has witnessed anything.
        """
        if state not in DESIRED_STATES:
            raise ValueError(
                f"{state!r} is not a desired state; expected "
                f"{sorted(DESIRED_STATES)}")
        spec = self._spec(runner_id)

        operation, created = self.operations.open(
            "set_desired", runner_id=runner_id, requested_by=requested_by,
            idempotency_key=idempotency_key,
            note=f"desired {spec['desired_state']} -> {state}")
        if not created:
            return operation["operation_id"]

        self.specs.update(runner_id, spec["spec_version"], desired_state=state)
        self.operations.succeed(operation["operation_id"], {"desired": state})
        return operation["operation_id"]

    # ---- verbs -------------------------------------------------------------

    def act(self, runner_id, verb, requested_by=None, idempotency_key=None):
        """Ask for a verb. Validates, records, and returns an operation id.

        Nothing is carried out here. The reconciler picks the operation up,
        which is what lets a thirty-second removal be watched rather than
        waited on.

        Every request is audited, accepted or refused (T-1802): a refused
        destroy is exactly what an operator needs to find later.
        """
        spec = self.specs.get(runner_id) if isinstance(runner_id, str)             else None
        fleet = (spec or {}).get("fleet_id")
        try:
            operation_id = self._act(runner_id, verb, requested_by,
                                     idempotency_key)
        except (Refused, UnknownRunner, ValueError) as e:
            self._audit(verb, "refused", requested_by, runner_id=runner_id,
                        fleet_id=fleet,
                        outcome=str(e) or f"no runner {runner_id}")
            raise
        self._audit(verb, "accepted", requested_by, runner_id=runner_id,
                    fleet_id=fleet, operation_id=operation_id)
        return operation_id

    def _audit(self, verb, decision, actor, **fields):
        """Never lets an audit failure change the answer - but an audit that
        cannot be written is itself worth a line in the log."""
        from . import audit
        try:
            audit.record(self.operations.path, verb, decision,
                         actor=actor or "unknown", **fields)
        except Exception as e:      # noqa: BLE001
            print(f"[audit] could not record {decision} {verb}: "
                  f"{type(e).__name__}")

    def _act(self, runner_id, verb, requested_by, idempotency_key):
        if verb not in states.VERBS:
            raise ValueError(f"unknown verb {verb!r}")
        if verb in states.READS:
            raise Refused(
                f"{verb} is a read, not an operation; call it directly "
                f"instead of asking for it to be scheduled")
        if verb in states.FLEET_VERBS:
            raise Refused(
                f"{verb} acts on a fleet's capacity, not on one runner; "
                f"use the fleet's capacity instead")

        spec = self._spec(runner_id)
        current = spec["actual_state"]

        # First, before any other judgement. A caller that did not hear the
        # answer repeats the call, and the repeat must find its own operation
        # rather than be told one is already in flight - which is what the
        # first call started. Checking anything else first would make the one
        # safe move a caller has look like a conflict.
        repeat = self._repeat(idempotency_key, runner_id, verb)
        if repeat:
            return repeat["operation_id"]

        if states.aborts_a_job(verb, current):
            raise Refused(
                f"{verb} would abort the job this runner is running "
                f"({current}); drain it first - drain lets the job finish "
                f"and takes no other" + self._why_not(verb, current))
        if not states.allows(verb, current) and not                 self._drains_first(spec, verb, current):
            raise Refused(
                f"{verb} is not possible while the runner is {current!r}"
                + self._why_not(verb, current))

        if spec["current_operation"]:
            in_flight = self.operations.get(spec["current_operation"])
            if in_flight and in_flight["state"] in ("pending", "running"):
                raise Refused(
                    f"{in_flight['verb']} is already in flight on this "
                    f"runner ({in_flight['operation_id']}); wait for it or "
                    f"cancel it")

        if verb == "recreate":
            self.validate_replacement(spec)

        operation, created = self.operations.open(
            verb, runner_id=runner_id, requested_by=requested_by,
            idempotency_key=idempotency_key,
            note=f"{verb} requested while {current}")
        if not created:
            return operation["operation_id"]

        changes = {"current_operation": operation["operation_id"]}
        if verb in DESIRED_BY_VERB:
            changes["desired_state"] = DESIRED_BY_VERB[verb]
        self.specs.update(runner_id, spec["spec_version"], **changes)

        return operation["operation_id"]

    def _repeat(self, idempotency_key, runner_id, verb):
        """The operation this key already opened, if there is one.

        A key that was used for a different verb or a different runner is not
        a repeat - it is a collision, and returning the other operation would
        answer a question nobody asked. Keys are caller-generated, so a
        collision means the caller is reusing one, and saying so is more use
        than a confusing success.
        """
        if not idempotency_key:
            return None
        existing = self.operations.by_key(idempotency_key)
        if not existing:
            return None
        if existing["verb"] != verb or existing["runner_id"] != runner_id:
            raise Refused(
                f"idempotency key {idempotency_key!r} was already used for "
                f"{existing['verb']} on {existing['runner_id']}; a key "
                f"identifies one request, not a family of them")
        return existing

    @staticmethod
    def _drains_first(spec, verb, current):
        """FR-17: a cache clear skips an active runner - or, when its cache
        policy says `drain-first`, drains it and clears once its job is done.
        Skipping is the default; the reconciler does the draining."""
        policy = spec.get("cache_policy") or {}
        return (verb == "clear_cache" and current in states.AT_WORK
                and policy.get("on_clear") == "drain-first")

    def _why_not(self, verb, current):
        if verb in states.GUARDED:
            return (f"; it needs the runner to be one of "
                    f"{sorted(states.GUARDED[verb])}")
        edges = states.VERB_EDGES.get(verb)
        if edges:
            froms = sorted(f for f, _ in edges if f)
            return f"; {verb} starts from {froms}"
        return ""

    # ---- the eighteen verbs -------------------------------------------------
    #
    # One method per verb of `uniform.md` 162-181, and every mutating one is a
    # single line into `act`. That is what "no verb is implemented twice for
    # different platforms" means in practice: there is nowhere for a Windows
    # version of `stop` to go. A test walks this class's source and fails on
    # any platform name appearing in a verb.
    #
    # The composites carry no logic here either. `restart` and `recreate` are
    # recorded as themselves, and what they decompose into is written once, in
    # `states.COMPOSITE`, for the provisioner to follow.

    def create(self, fid, count=1, requested_by=None, idempotency_key=None):
        """Add runners to a fleet's persisted desired count.

        Not a direct `plan`, and the first version was one. A runner planned
        straight into existence while the fleet still wanted zero was withdrawn
        by the very next reconciler pass - two sources of truth for "how many"
        fighting each other, with the operator's click losing. Capacity is the
        only source, so creating a runner raises it and the reconciler takes
        the `create` edge into `planned` itself.

        `plan` stays as the primitive the reconciler uses to fill a gap. It is
        not a way round capacity.
        """
        if count < 1:
            raise ValueError("create needs a positive count")
        return self._scale(fid, count, requested_by, idempotency_key)

    def adopt(self, fid, display_name, host_id, registration, unit,
              requested_by=None):
        """Take a runner that is already serving into this fleet (T-0802).

        Intent only, like everything else here: the spec is born `planned`
        with the forge's own identifiers for the runner and with the unit it
        already is, and the reconciler provisions it the ordinary way -
        which, for a unit that exists and a registration the forge still
        holds, means adopting the one and keeping the other. Nothing is
        registered, rebuilt or restarted by adopting.

        The fleet's capacity rises with it, in the same transaction, because
        a runner nobody counted is a runner the next pass withdraws.
        """
        fleet = self.fleets.get(fid)
        if fleet is None:
            raise Refused(f"no fleet {fid}")
        already = [s for s in self.specs.list(fleet_id=fid)
                   if s.get("display_name") == display_name
                   and s["actual_state"] != "absent"]
        if already:
            return already[0]["runner_id"]

        runner_id = self.specs.adopt(
            fleet["desired_capacity"] + 1,
            display_name=str(display_name),
            provider=fleet["provider"], platform=fleet["platform"],
            architecture=fleet["architecture"],
            runtime_template=fleet.get("template"),
            labels=fleet.get("labels") or [],
            runner_group=fleet.get("runner_group"),
            cache_policy=fleet.get("cache_policy"),
            fleet_id=fid, host_id=host_id,
            desired_state="running", actual_state="planned",
            registration_id=registration.get("id"),
            registration_uuid=registration.get("uuid"),
            adopt_unit=dict(unit))
        self._audit("adopt", "accepted", requested_by, runner_id=runner_id,
                    fleet_id=fid,
                    parameters={"display_name": display_name,
                                "registration_id": registration.get("id"),
                                "unit": unit.get("label")})
        return runner_id

    def provision(self, runner_id, **kw):
        return self.act(runner_id, "provision", **kw)

    def register(self, runner_id, **kw):
        return self.act(runner_id, "register", **kw)

    def start(self, runner_id, **kw):
        return self.act(runner_id, "start", **kw)

    def stop(self, runner_id, **kw):
        return self.act(runner_id, "stop", **kw)

    def restart(self, runner_id, **kw):
        """Stop, then start - once, in `states.COMPOSITE`."""
        return self.act(runner_id, "restart", **kw)

    def drain(self, runner_id, **kw):
        return self.act(runner_id, "drain", **kw)

    def cancel_drain(self, runner_id, **kw):
        return self.act(runner_id, "cancel_drain", **kw)

    def recreate(self, runner_id, **kw):
        """Remove keeping the data volume, then create. Keeping the data is
        the entire difference from `remove` followed by `create`."""
        return self.act(runner_id, "recreate", **kw)

    def remove(self, runner_id, **kw):
        """The machine's own edge: take this unit away. Refused where the
        machine refuses it, which is anywhere a runner could still be
        serving. What an operator means by "remove" is `retire`."""
        return self.act(runner_id, "remove", **kw)

    def retire(self, runner_id, requested_by=None, idempotency_key=None):
        """This runner may go: one runner fewer, gracefully.

        What an operator means by remove, and the mirror of `create`. Two
        things had to be true for the word to mean that.

        It is `desired_state = absent`, not the machine's `remove` edge: a
        runner that is serving cannot be removed where it stands, and asking
        for that was refused with a state machine's words. The reconciler
        takes it from here - drain, deregister, remove - which is the
        graceful path the design already had.

        And it lowers what the fleet wants. The desired count is what the controller
        keeps true, so a removal that left it alone was a removal the next
        pass undid: the runner came back. A recreate, a scale-down's own
        victims and the reconciler's own work all go elsewhere, so this is
        the one path that shrinks a fleet (2026-09-20).
        """
        spec = self._spec(runner_id)
        operation = self.set_desired(runner_id, "absent",
                                     requested_by=requested_by,
                                     idempotency_key=idempotency_key)
        fleet = self.fleets.get(spec.get("fleet_id") or "")
        if (fleet and spec["desired_state"] != "absent"
                and fleet["desired_capacity"] > 0):
            self.fleets.set_capacity(
                fleet["fleet_id"], fleet["desired_capacity"] - 1,
                requested_by=requested_by,
                idempotency_key=f"retire:{runner_id}:"
                                f"{fleet['desired_capacity']}")
        return operation

    def deregister(self, runner_id, **kw):
        return self.act(runner_id, "deregister", **kw)

    def repair(self, runner_id, **kw):
        """The `failed -> provisioning` edge. Also what `reconcile` means for
        one runner: make it what its spec says, from wherever it is."""
        return self.act(runner_id, "repair", **kw)

    def clear_cache(self, runner_id, **kw):
        """Refused unless idle or drained; see `states.GUARDED`."""
        return self.act(runner_id, "clear_cache", **kw)

    def scale_up(self, fid, by=1, requested_by=None, idempotency_key=None):
        return self._scale(fid, by, requested_by, idempotency_key)

    def scale_down(self, fid, by=1, requested_by=None, idempotency_key=None):
        """Lowers the target. The reconciler chooses which runner goes, and
        only an idle or drained one - a scale-down never aborts a job."""
        return self._scale(fid, -by, requested_by, idempotency_key)

    def set_capacity(self, fid, desired, requested_by=None,
                     idempotency_key=None):
        """Scale up and scale down are this one call with a different number
        (design 14.3, `uniform.md` 158). Audited, accepted or refused."""
        try:
            operation_id = self.fleets.set_capacity(
                fid, desired, requested_by=requested_by,
                idempotency_key=idempotency_key)
        except ValueError as e:
            self._audit("set_capacity", "refused", requested_by,
                        fleet_id=fid, outcome=str(e),
                        parameters={"desired": desired})
            raise
        except Exception as e:      # FleetUnavailable, UnknownFleet
            self._audit("set_capacity", "refused", requested_by,
                        fleet_id=fid, outcome=str(e),
                        parameters={"desired": desired})
            raise Refused(str(e)) from e
        self._audit("set_capacity", "accepted", requested_by, fleet_id=fid,
                    operation_id=operation_id,
                    parameters={"desired": desired})
        return operation_id

    def _scale(self, fid, delta, requested_by, idempotency_key=None):
        if delta == 0:
            raise ValueError("scaling by zero is not a change")
        # A repeat first, before the target is worked out again from a
        # capacity the first call already changed: "one more", repeated,
        # must not become two more - or be refused as going below zero.
        if idempotency_key:
            repeat = self.operations.by_key(idempotency_key)
            if repeat:
                return repeat["operation_id"]
        fleet = self.fleets.get(fid)
        if fleet is None:
            raise Refused(f"no fleet {fid}")
        target = fleet["desired_capacity"] + delta
        if target < 0:
            raise Refused(
                f"{fid} wants {fleet['desired_capacity']}; it cannot go "
                f"{abs(delta)} lower")
        return self.set_capacity(fid, target, requested_by, idempotency_key)

    # ---- reads -------------------------------------------------------------
    #
    # The three reads ARE synchronous, and that is not a lapse in "the service
    # never performs work". Design 12.2 calls them reads and 18.5 says logs are
    # fetched on demand: there is nothing to schedule, nothing to retry, and an
    # operator asking for logs wants them now. They change nothing, so the
    # properties operations exist for - safe retry, observable progress,
    # recovery after a crash - have nothing to protect. A test asserts these
    # three are the only methods that construct a runtime.

    def fetch_status(self, runner_id):
        """What the spec says, and what the execution unit says, side by side.

        Both, because they can disagree, and the disagreement is the
        interesting part - it is what the reconciler exists to close.
        """
        spec = self._spec(runner_id)
        observed = None
        if spec["exec_unit_ref"]:
            runtime, ref = self._runtime_and_ref(spec)
            observed = retry.call(retry.agent_fast_for(spec), runtime.status, ref)
        return {"runner_id": runner_id,
                "desired_state": spec["desired_state"],
                "actual_state": spec["actual_state"],
                "current_operation": spec["current_operation"],
                "last_error": spec["last_error"],
                "observed": observed}

    def fetch_logs(self, runner_id, since_seconds=300):
        spec = self._spec(runner_id)
        if not spec["exec_unit_ref"]:
            return ""
        runtime, ref = self._runtime_and_ref(spec)
        return retry.call(retry.agent_fast_for(spec), runtime.logs, ref,
                          since_seconds=since_seconds)

    def inspect_resources(self, runner_id):
        spec = self._spec(runner_id)
        if not spec["exec_unit_ref"]:
            return None
        runtime, ref = self._runtime_and_ref(spec)
        return retry.call(retry.agent_fast_for(spec), runtime.telemetry, ref)

    def _runtime_and_ref(self, spec):
        from runtime.base import ExecUnitKind, ExecUnitRef
        runtime = self.runtime(spec)
        ref = ExecUnitRef(kind=ExecUnitKind(EXEC_KINDS[spec["platform"]]),
                          handle=spec["exec_unit_ref"],
                          runner_id=spec["runner_id"])
        return runtime, ref

    # ---- helpers -----------------------------------------------------------

    def _spec(self, runner_id):
        spec = self.specs.get(runner_id)
        if spec is None:
            raise UnknownRunner(runner_id)
        return spec
