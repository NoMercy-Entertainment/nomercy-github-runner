"""Start the agent on a worker: `python -m agent --config <file>`.

Reads the worker's configuration (agent/config.py), builds the runtime it
names, and serves the closed verb set over mutual TLS on the management
address, sending a heartbeat every ten seconds and an event for every
operation. It runs until it is told to stop - SIGTERM from systemd or launchd,
Ctrl+C from the Windows service manager - and then stops serving, lets the
heartbeat go quiet, and exits. Runners are not touched on the way out: they
are units of their own, and an agent restart must not become a fleet restart.

Nothing is served until every part has been built. A certificate that does not
load, or a runtime that cannot be made, stops the agent with the reason before
it opens a port.
"""
import argparse
import signal
import sys
import threading

from . import tls
from .config import ConfigError, load
from .heartbeat import HeartbeatSender
from .link import ControllerLink, EventSender
from .protocol import HEARTBEAT_PATH
from .server import AgentServer
from .verbs import Agent


def _secret(path):
    """A credential read from its own file, so the configuration holds the
    path and never the secret. Nothing else reads it: it goes straight into
    the environment of the one process that needs it."""
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        return fh.read().strip()


def runtime_for(config):
    """(runtime, registrar) for the runtime the configuration names."""
    if config.runtime == "linux-container":
        from .runtimes.linux_container import (LinuxContainerRuntime,
                                               LinuxRegistrar)
        return LinuxContainerRuntime(storage=config.storage), LinuxRegistrar()
    if config.runtime == "windows-process":
        from .runtimes.windows_process import (WindowsProcessRuntime,
                                               WindowsRegistrar)
        runtime = WindowsProcessRuntime(tools=config.tools, storage=config.windows_storage)
        return runtime, WindowsRegistrar(tools=config.tools, storage_backend=runtime._storage)
    if config.runtime == "macos-appliance":
        if config.appliance_pool:
            if not sys.platform.startswith("linux"):
                raise ConfigError("appliance_pool requires a Linux KVM host")
            from .runtimes.macos_pool import MacAppliancePoolRuntime, MacPoolRegistrar
            guest = dict(config.guest, password=_secret(config.guest.get("password_file")))
            pool = MacAppliancePoolRuntime(**config.appliance_pool, guest=guest, tools=config.tools)
            return pool, MacPoolRegistrar(pool)
        # Inside the guest there is no hypervisor to reach: the appliance
        # host is the other side's (T-0803), so none is given here.
        from .runtimes.macos_appliance import (MacApplianceRuntime,
                                               MacRegistrar)
        if not config.guest:
            return MacApplianceRuntime(tools=config.tools), MacRegistrar()
        # On the appliance host instead: every call acts inside the guest,
        # over the SSH port the host forwards.
        from .runtimes.guest_ssh import GuestExec, GuestFs
        run = GuestExec(host=config.guest["host"], user=config.guest["user"],
                        port=config.guest.get("port", 22),
                        key=config.guest.get("key"),
                        password=_secret(config.guest.get("password_file")),
                        ssh=config.guest.get("ssh", "ssh"),
                        sshpass=config.guest.get("sshpass", "sshpass"))
        fs = GuestFs(run)
        appliance = None
        if config.appliance:
            from .runtimes.appliance_host import DockerApplianceHost
            appliance = DockerApplianceHost(guest=run, **config.appliance)
        return (MacApplianceRuntime(run=run, fs=fs, tools=config.tools,
                                    appliance=appliance, remote=True),
                MacRegistrar(run=run, fs=fs))
    raise ConfigError(f"no runtime {config.runtime!r}")      # pragma: no cover


class Declared:
    """A runtime whose capabilities also say what this worker can hold.

    Capacity is the worker's, not the runtime's: the same runtime runs on a
    16 GB worker and on a 160 GB one. It is added to what the runtime says,
    and every other call goes straight through."""

    def __init__(self, runtime, capacity):
        self._runtime = runtime
        self._capacity = dict(capacity)

    def capabilities(self):
        caps = dict(self._runtime.capabilities() or {}, **self._capacity)
        if not hasattr(self._runtime, "memory_capacity"):
            return caps
        measured = self._runtime.memory_capacity()
        if measured is None:
            return dict(caps, capacity_valid=False, capacity_error="worker memory could not be measured")
        physical, swap = measured["memory_bytes"], measured["swap_bytes"]
        caps["memory_total_bytes"], caps["swap_total_bytes"] = physical, swap
        memory_budget = self._capacity.get("memory_bytes", physical)
        swap_budget = self._capacity.get("swap_bytes", swap)
        commit_budget = self._capacity.get("memory_commit_bytes", memory_budget)
        caps["memory_bytes"] = min(memory_budget, physical)
        caps["swap_bytes"] = min(swap_budget, swap)
        caps["capacity_valid"] = (memory_budget <= physical and swap_budget <= swap
                                  and commit_budget <= caps["memory_bytes"] + caps["swap_bytes"])
        if "memory_commit_bytes" in self._capacity and self._capacity.get("memory_admission") != "bounded-overcommit":
            caps["capacity_valid"] = False
        if not caps["capacity_valid"]:
            caps["capacity_error"] = "configured memory or commitment exceeds measured worker capacity"
        return caps

    def __getattr__(self, name):
        return getattr(self._runtime, name)


class Running:
    """The agent's parts, started together and stopped together."""

    def __init__(self, config, runtime=None, registrar=None):
        if runtime is None or registrar is None:
            runtime, registrar = runtime_for(config)
        if config.capacity or config.runtime == "linux-container":
            runtime = Declared(runtime, config.capacity)
        self.config = config
        self.agent = Agent(config.host_id, runtime, registrar,
                           version=config.version, permitted=config.permitted)
        outbound = tls.client_context(config.cert, config.key, config.ca)
        self.events = EventSender(ControllerLink(config.controller, outbound))
        self.server = AgentServer(
            self.agent, address=config.listen,
            ssl_context=tls.server_context(config.cert, config.key,
                                           config.ca),
            emit=self.events)
        self.heartbeat = HeartbeatSender(
            self.agent, config.controller + HEARTBEAT_PATH, outbound,
            server=self.server)

    def start(self):
        self.server.start()
        self.heartbeat.start()
        return self

    def stop(self):
        self.heartbeat.stop()
        self.server.stop()


def main(argv=None, until=None):
    parser = argparse.ArgumentParser(prog="python -m agent")
    parser.add_argument("--config", required=True,
                        help="the worker's configuration file")
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
        running = Running(config)
    except (ConfigError, OSError, ValueError) as e:
        print(f"agent: not started: {e}", file=sys.stderr)
        return 2

    stop = until or threading.Event()

    def asked_to_stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGTERM, asked_to_stop)
    signal.signal(signal.SIGINT, asked_to_stop)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, asked_to_stop)

    running.start()
    host, port = config.listen
    print(f"agent: {config.host_id} ({config.runtime}) serving on "
          f"{host}:{port}, reporting to {config.controller}", flush=True)
    try:
        # A timed wait, so a signal is handled promptly on every platform.
        while not stop.wait(1):
            pass
    finally:
        running.stop()
        print("agent: stopped; the runners were left running", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
