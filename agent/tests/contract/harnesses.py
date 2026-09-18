"""One harness per runtime: what the suite needs to know about a platform.

A harness owns a fake world for its runtime - an engine, a forge - and answers
the suite's questions about it. It is the only place that knows how the
platform is built. A new runtime joins the suite by adding one here to
`HARNESSES`; `suite.py` does not change.
"""
import uuid

from agent import naming
from agent.runtimes.linux_container import (LinuxContainerRuntime,
                                            LinuxRegistrar)

from ..fake_docker import FakeDocker
from .suite import NotSupported


class LinuxContainerHarness:
    name = "linux-container"
    platform = "linux"

    def __init__(self):
        self.docker = FakeDocker()
        self.runtime = LinuxContainerRuntime(run=self.docker)
        self.registrar = LinuxRegistrar(run=self.docker)

    # ---- inputs -------------------------------------------------------------

    def new_id(self):
        return str(uuid.uuid4())

    def spec(self):
        return {"image": "ghcr.io/nomercy/runner:latest", "memory": "8g"}

    def plan(self, runner_id):
        return {"url": "https://git.example", "token": "tok-contract-123456",
                "name": f"rnr-{runner_id[:8]}", "labels": "self-hosted"}

    # ---- the forge ----------------------------------------------------------

    def forge_records(self, runner_id):
        return self.docker.forge.for_unit(naming.unit_name(runner_id))

    def forge_online(self, registration_id):
        return self.docker.forge.online(registration_id)

    def forge_reachable(self, reachable):
        self.docker.forge.reachable = reachable

    # ---- the unit and its storage -------------------------------------------

    def expected_areas(self):
        return {a for a, n in naming.names(str(uuid.uuid4()),
                                           self.platform).items() if n}

    def storage_present(self, runner_id):
        return {area for area, name in naming.names(runner_id,
                                                    self.platform).items()
                if name and name in self.docker.volumes}

    def unit_present(self, runner_id):
        return naming.unit_name(runner_id) in self.docker.containers

    def units(self, runner_id):
        return sum(1 for n in self.docker.containers
                   if n == naming.unit_name(runner_id))

    def snapshot(self, runner_id):
        everything = self.docker.snapshot()
        return {n: everything.get(n)
                for n in naming.names(runner_id, self.platform).values() if n}

    def put_cache(self, runner_id, size):
        self.docker.engine(naming.names(runner_id, self.platform)["docker"],
                           build_cache=size)

    def cache_kept(self, runner_id):
        volume = self.docker.volumes.get(
            naming.names(runner_id, self.platform)["docker"])
        return bool(volume) and volume["engine"]["build_cache"] > 0

    def mark_workspace(self, runner_id):
        self.docker.put(naming.names(runner_id, self.platform)["work"],
                        "marker", 1)

    def workspace_marked(self, runner_id):
        volume = self.docker.volumes.get(
            naming.names(runner_id, self.platform)["work"])
        return bool(volume) and "marker" in volume["files"]

    # ---- faults -------------------------------------------------------------

    def crash_during_create(self):
        """The container is made, then the agent dies before it can say so."""
        self.docker.crash_after = "run"

    # ---- drain: OPEN-7 -------------------------------------------------------

    def _no_drain(self, *_):
        raise NotSupported("OPEN-7: nothing can drain a Linux runner yet")

    drain = cancel_drain = start_job = finish_job = offer_job = _no_drain


#: Every runtime the suite runs against. Windows and macOS are added here in
#: phases 6 and 7.
HARNESSES = {"linux-container": LinuxContainerHarness}