"""A worker's configuration: who it is, what it drives, and where to report.

One JSON file per worker, written when the worker is enrolled. Everything the
agent needs to start is in it, and nothing it must not have: no forge token -
the controller mints a registration token per runner and sends it with the
request that needs it - and no command. Which programs a runtime runs is
fixed in its own code; `tools` only says where they are on this machine.

Every field is checked before anything starts. An agent that started on a
half-understood configuration would serve with defaults nobody chose, and the
first sign of it would be a runner in the wrong place.

    {
      "host_id": "linux-worker-1",
      "runtime": "linux-container",
      "listen": "10.77.0.20:8443",
      "controller": "https://10.77.0.10:8444",
      "tls": {"cert": "/etc/runner-agent/agent.crt",
              "key": "/etc/runner-agent/agent.key",
              "ca": "/etc/runner-agent/ca.pem"},
      "permitted": ["exec_unit.status", ...],        optional: every verb
      "capacity": {"max_runners": 2,                 optional: unlimited
                   "memory_bytes": 12884901888},
      "tools": {"nssm": "C:\\\\ProgramData\\\\nomercy\\\\nssm.exe"},  optional
      "version": "2026.09.18"                        optional
    }
"""
import ipaddress
import json
import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple
from urllib.parse import urlsplit

from . import protocol

RUNTIMES = ("linux-container", "windows-process", "macos-appliance")
FIELDS = frozenset({"host_id", "runtime", "listen", "controller", "tls",
                    "permitted", "tools", "version", "capacity"})
#: What a worker may declare it can hold. Placement reads these and never
#: puts a runner where it would not fit (dashboard/control/placement.py).
CAPACITY_FIELDS = frozenset({"max_runners", "memory_bytes", "architecture"})
TLS_FIELDS = frozenset({"cert", "key", "ca"})


class ConfigError(ValueError):
    """The configuration cannot be served as written, and why."""


@dataclass(frozen=True)
class Config:
    host_id: str
    runtime: str
    listen: Tuple[str, int]
    controller: str
    cert: str
    key: str
    ca: str
    permitted: Optional[frozenset] = None
    tools: Mapping[str, str] = field(default_factory=dict)
    version: str = "0"
    capacity: Mapping[str, object] = field(default_factory=dict)


def _listen(text):
    host, sep, port = str(text or "").rpartition(":")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise ConfigError(f"listen must be address:port, not {text!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]                   # [::1]:8443
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(f"listen must name an IP address, not {host!r}")
    if address.is_unspecified:
        # 13.2: the agent listens only on the worker's management address.
        raise ConfigError("listen on the management address, not on every "
                          "address")
    return str(address), int(port)


def parse(data, exists=os.path.isfile):
    """A Config from the decoded file, or ConfigError naming the problem."""
    if not isinstance(data, dict):
        raise ConfigError("the configuration is not a JSON object")
    unknown = sorted(set(data) - FIELDS)
    if unknown:
        raise ConfigError(f"fields this agent does not take: {unknown}")
    for name in ("host_id", "runtime", "listen", "controller", "tls"):
        if not data.get(name):
            raise ConfigError(f"{name} is required")

    host_id = str(data["host_id"])
    if not host_id.replace("-", "").replace(".", "").isalnum() or \
            len(host_id) > 64:
        raise ConfigError(f"host_id {host_id!r} is not a worker name")
    if data["runtime"] not in RUNTIMES:
        raise ConfigError(f"runtime must be one of {list(RUNTIMES)}")

    controller = str(data["controller"]).rstrip("/")
    parts = urlsplit(controller)
    if parts.scheme != "https" or not parts.hostname or parts.path:
        raise ConfigError("controller must be https://host:port and nothing "
                          "more; heartbeats and events go over TLS only")

    tls = data["tls"]
    if not isinstance(tls, dict) or set(tls) != TLS_FIELDS:
        raise ConfigError(f"tls must name exactly {sorted(TLS_FIELDS)}")
    for name in sorted(TLS_FIELDS):
        if not exists(str(tls[name])):
            raise ConfigError(f"tls {name} file not found: {tls[name]}")

    permitted = data.get("permitted")
    if permitted is not None:
        if not isinstance(permitted, list):
            raise ConfigError("permitted must be a list of verbs")
        bad = sorted(set(permitted) - protocol.VERB_NAMES)
        if bad:
            raise ConfigError(f"not protocol verbs: {bad}")
        permitted = frozenset(permitted)

    tools = data.get("tools") or {}
    if not isinstance(tools, dict) or not all(
            isinstance(k, str) and isinstance(v, str)
            for k, v in tools.items()):
        raise ConfigError("tools must map names to paths")

    capacity = data.get("capacity") or {}
    if not isinstance(capacity, dict) or set(capacity) - CAPACITY_FIELDS:
        raise ConfigError(f"capacity may name only "
                          f"{sorted(CAPACITY_FIELDS)}")
    for name in ("max_runners", "memory_bytes"):
        if name in capacity and (not isinstance(capacity[name], int)
                                 or isinstance(capacity[name], bool)
                                 or capacity[name] < 1):
            raise ConfigError(f"capacity.{name} must be a positive whole "
                              f"number")
    if "architecture" in capacity and capacity["architecture"] not in (
            "x64", "arm64"):
        raise ConfigError("capacity.architecture must be x64 or arm64")

    return Config(host_id=host_id, runtime=data["runtime"],
                  listen=_listen(data["listen"]), controller=controller,
                  cert=str(tls["cert"]), key=str(tls["key"]),
                  ca=str(tls["ca"]), permitted=permitted, tools=dict(tools),
                  version=str(data.get("version") or "0"),
                  capacity=dict(capacity))


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror or e}")
    except ValueError as e:
        raise ConfigError(f"{path} is not JSON: {e}")
    return parse(data)
