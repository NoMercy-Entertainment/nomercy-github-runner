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
from agent.runtimes.macos_appliance import MacApplianceRuntime, MacRegistrar
from agent.runtimes.windows_process import (WindowsProcessRuntime,
                                            WindowsRegistrar)

from ..fake_docker import FakeDocker
from ..fake_macos import TEMPLATE as MAC_TEMPLATE
from ..fake_macos import TOOLS as MAC_TOOLS
from ..fake_macos import FakeMac
from ..fake_windows import TEMPLATE as WINDOWS_TEMPLATE
from ..fake_windows import TOOLS as WINDOWS_TOOLS
from ..fake_windows import FakeWindows
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


class WindowsProcessHarness:
    name = "windows-process"
    platform = "windows"

    def __init__(self):
        self.host = FakeWindows()
        self.runtime = WindowsProcessRuntime(run=self.host, fs=self.host,
                                             tools=WINDOWS_TOOLS)
        self.registrar = WindowsRegistrar(run=self.host, fs=self.host,
                                          tools=WINDOWS_TOOLS)

    # ---- inputs -------------------------------------------------------------

    def new_id(self):
        return str(uuid.uuid4())

    def spec(self):
        return {"image": WINDOWS_TEMPLATE, "memory": "8g", "cpus": "4"}

    def plan(self, runner_id):
        return {"url": "https://git.example", "token": "tok-contract-123456",
                "name": f"rnr-{runner_id[:8]}", "labels": "self-hosted"}

    # ---- the forge ----------------------------------------------------------

    def forge_records(self, runner_id):
        return self.host.forge.for_unit(naming.unit_name(runner_id))

    def forge_online(self, registration_id):
        return self.host.forge.online(registration_id)

    def forge_reachable(self, reachable):
        self.host.forge.reachable = reachable

    # ---- the unit and its storage -------------------------------------------

    def _paths(self, runner_id):
        return self.runtime.paths(runner_id)

    def expected_areas(self):
        return {a for a, n in naming.names(str(uuid.uuid4()),
                                           self.platform).items() if n}

    def storage_present(self, runner_id):
        return {area for area, path in naming.names(runner_id,
                                                    self.platform).items()
                if path and self.host.exists(path)}

    def unit_present(self, runner_id):
        return naming.unit_name(runner_id) in self.host.services

    def units(self, runner_id):
        return sum(1 for n in self.host.services
                   if n == naming.unit_name(runner_id))

    def snapshot(self, runner_id):
        return self.host.snapshot(self._paths(runner_id)["root"])

    def put_cache(self, runner_id, size):
        self.host.put(self._paths(runner_id)["cache"] + r"\tools\node.zip",
                      size)

    def cache_kept(self, runner_id):
        return self.host.size_under(self._paths(runner_id)["cache"]) > 0

    def mark_workspace(self, runner_id):
        self.host.put(self._paths(runner_id)["work"] + r"\marker", 1)

    def workspace_marked(self, runner_id):
        return self.host.exists(self._paths(runner_id)["work"] + r"\marker")

    # ---- faults -------------------------------------------------------------

    def crash_during_create(self):
        """The service is installed, then the agent dies before it is
        configured or started."""
        self.host.crash_after = "install"

    # ---- drain: OPEN-7 -------------------------------------------------------

    def _no_drain(self, *_):
        raise NotSupported("OPEN-7: nothing can drain a Windows runner yet")

    drain = cancel_drain = start_job = finish_job = offer_job = _no_drain


class MacApplianceHarness:
    name = "macos-appliance"
    platform = "macos"

    def __init__(self):
        self.guest = FakeMac()
        self.runtime = MacApplianceRuntime(run=self.guest, fs=self.guest,
                                           appliance=self.guest.appliance,
                                           tools=MAC_TOOLS)
        self.registrar = MacRegistrar(run=self.guest, fs=self.guest)

    # ---- inputs -------------------------------------------------------------

    def new_id(self):
        return str(uuid.uuid4())

    def spec(self):
        return {"image": MAC_TEMPLATE}

    def plan(self, runner_id):
        return {"url": "https://git.example", "token": "tok-contract-123456",
                "name": f"rnr-{runner_id[:8]}", "labels": "macos:host"}

    # ---- the forge ----------------------------------------------------------

    def forge_records(self, runner_id):
        return self.guest.forge.for_unit(naming.unit_name(runner_id))

    def forge_online(self, registration_id):
        return self.guest.forge.online(registration_id)

    def forge_reachable(self, reachable):
        self.guest.forge.reachable = reachable

    # ---- the instance and its storage ---------------------------------------

    def _paths(self, runner_id):
        return self.runtime.paths(runner_id)

    def expected_areas(self):
        return {a for a, n in naming.names(str(uuid.uuid4()),
                                           self.platform).items() if n}

    def storage_present(self, runner_id):
        return {area for area, path in naming.names(runner_id,
                                                    self.platform).items()
                if path and self.guest.exists(path)}

    def unit_present(self, runner_id):
        return self.guest.exists(self._paths(runner_id)["plist"]) or \
            self.runtime.label(runner_id) in self.guest.jobs

    def units(self, runner_id):
        return sum(1 for label in self.guest.jobs
                   if label == self.runtime.label(runner_id))

    def snapshot(self, runner_id):
        return self.guest.snapshot(self._paths(runner_id)["root"])

    def put_cache(self, runner_id, size):
        self.guest.put(self._paths(runner_id)["cache"] + "/tools/node.tgz",
                       size)

    def cache_kept(self, runner_id):
        return self.guest.size_under(self._paths(runner_id)["cache"]) > 0

    def mark_workspace(self, runner_id):
        self.guest.put(self._paths(runner_id)["work"] + "/marker", 1)

    def workspace_marked(self, runner_id):
        return self.guest.exists(self._paths(runner_id)["work"] + "/marker")

    # ---- faults -------------------------------------------------------------

    def crash_during_create(self):
        """The tree and the job file are laid down, then the agent dies as it
        asks launchd about the job - before it is ever loaded."""
        self.guest.crash_after = "print"

    # ---- drain: OPEN-7 -------------------------------------------------------

    def _no_drain(self, *_):
        raise NotSupported("OPEN-7: nothing can drain a macOS runner yet")

    drain = cancel_drain = start_job = finish_job = offer_job = _no_drain


#: Every runtime the suite runs against.
HARNESSES = {"linux-container": LinuxContainerHarness,
             "windows-process": WindowsProcessHarness,
             "macos-appliance": MacApplianceHarness}