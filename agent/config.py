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
import ntpath
import re
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple
from urllib.parse import urlsplit

from . import protocol

RUNTIMES = ("linux-container", "windows-process", "macos-appliance")
FIELDS = frozenset({"host_id", "runtime", "listen", "controller", "tls",
                    "permitted", "tools", "version", "capacity", "guest", "appliance", "appliance_pool", "storage", "windows_storage"})
APPLIANCE_FIELDS = frozenset({"name", "docker", "boot_timeout"})
POOL_FIELDS = frozenset({"image", "base_disk", "base_system", "nvram_seed", "data_root", "templates",
                         "base_guests_disabled", "ssh_port_base", "docker", "qemu_img",
                         "boot_timeout", "shutdown_timeout", "image_uid", "image_gid"})
TOOLS_FIELDS = {
    "linux-container": frozenset(),
    "windows-process": frozenset({"nssm", "sc", "icacls", "powershell", "python", "templates"}),
    "macos-appliance": frozenset({"launchctl", "ps", "df", "templates", "launch_agents", "domain", "runner_user"}),
}
#: What a worker may declare it can hold. Placement reads these and never
#: puts a runner where it would not fit (dashboard/control/placement.py).
CAPACITY_FIELDS = frozenset({"max_runners", "memory_bytes", "swap_bytes", "memory_commit_bytes", "memory_admission", "architecture"})
TLS_FIELDS = frozenset({"cert", "key", "ca"})
#: How an appliance host reaches the guest it drives. Only an appliance has
#: one: the other runtimes act on the machine the agent runs on. The
#: credential is a path, never the secret itself - the same rule that keeps
#: forge tokens out of this file.
GUEST_FIELDS = frozenset({"host", "user", "port", "key", "password_file",
                          "ssh", "sshpass"})


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
    guest: Mapping[str, object] = field(default_factory=dict)
    appliance: Mapping[str, object] = field(default_factory=dict)
    appliance_pool: Mapping[str, object] = field(default_factory=dict)
    storage: Mapping[str, object] = field(default_factory=dict)
    windows_storage: Mapping[str, object] = field(default_factory=dict)


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


def _guest(guest, runtime, exists):
    """The appliance guest block, checked whole. Empty when there is none,
    which means the agent runs inside the guest and acts on its own disk."""
    if not guest:
        return {}
    if runtime != "macos-appliance":
        raise ConfigError("only a macos-appliance runtime drives a guest; "
                          "every other runtime acts on its own machine")
    if not isinstance(guest, dict) or set(guest) - GUEST_FIELDS:
        raise ConfigError(f"guest may name only {sorted(GUEST_FIELDS)}")
    for name in ("host", "user"):
        if not isinstance(guest.get(name), str) or not guest[name] or any(
                char.isspace() or ord(char) < 32 for char in guest[name]) or guest[name].startswith("-"):
            raise ConfigError(f"guest {name} is required")
    port = guest.get("port", 22)
    if isinstance(port, bool) or not isinstance(port, int)             or not 0 < port < 65536:
        raise ConfigError(f"guest port must be a port number, not {port!r}")
    if not guest.get("key") and not guest.get("password_file"):
        raise ConfigError("guest must name a key or password_file: the agent "
                          "never asks a human for a credential")
    for name in ("key", "password_file"):
        if guest.get(name) and not exists(str(guest[name])):
            raise ConfigError(f"guest {name} file not found: {guest[name]}")
    return dict(guest, port=port)


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
    if set(tools) - TOOLS_FIELDS[data["runtime"]]:
        raise ConfigError("tools contains names unsupported by this runtime")
    if data["runtime"] == "macos-appliance":
        domain = tools.get("domain")
        if domain is not None and not re.fullmatch(r"system|(?:gui|user)/[0-9]+", domain):
            raise ConfigError("tools.domain must be system, gui/uid, or user/uid")
        user = tools.get("runner_user")
        if (user is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", user)) or (
                domain == "system" and not user):
            raise ConfigError("system launchd jobs need a valid tools.runner_user")

    capacity = data.get("capacity") or {}
    if not isinstance(capacity, dict) or set(capacity) - CAPACITY_FIELDS:
        raise ConfigError(f"capacity may name only "
                          f"{sorted(CAPACITY_FIELDS)}")
    for name in ("max_runners", "memory_bytes", "swap_bytes", "memory_commit_bytes"):
        if name in capacity and (not isinstance(capacity[name], int)
                                 or isinstance(capacity[name], bool)
                                 or capacity[name] < (0 if name == "swap_bytes" else 1)):
            raise ConfigError(f"capacity.{name} must be a positive whole "
                              f"number")
    if "architecture" in capacity and capacity["architecture"] not in (
            "x64", "arm64"):
        raise ConfigError("capacity.architecture must be x64 or arm64")
    if "memory_admission" in capacity and capacity["memory_admission"] not in (
            "physical", "bounded-overcommit"):
        raise ConfigError("capacity.memory_admission must be physical or bounded-overcommit")
    if "memory_commit_bytes" in capacity and capacity.get("memory_admission") != "bounded-overcommit":
        raise ConfigError("a memory commitment budget requires explicit bounded-overcommit admission")

    guest = _guest(data.get("guest"), data["runtime"], exists)
    appliance = data.get("appliance") or {}
    if not isinstance(appliance, dict) or set(appliance) - APPLIANCE_FIELDS:
        raise ConfigError(f"appliance may name only {sorted(APPLIANCE_FIELDS)}")
    if appliance:
        if data["runtime"] != "macos-appliance" or not guest:
            raise ConfigError("appliance power control requires a macOS guest connection")
        if not isinstance(appliance.get("name"), str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", appliance["name"]):
            raise ConfigError("appliance.name must name exactly one execution unit")
        deadline = appliance.get("boot_timeout", 600)
        if isinstance(deadline, bool) or not isinstance(deadline, int) or not 10 <= deadline <= 900:
            raise ConfigError("appliance.boot_timeout must be 10..900 seconds")
        if "docker" in appliance and (not isinstance(appliance["docker"], str)
                                      or not appliance["docker"]):
            raise ConfigError("appliance.docker must be an executable path")
        appliance = dict(appliance, boot_timeout=deadline)

    pool = data.get("appliance_pool") or {}
    if not isinstance(pool, dict) or set(pool) - POOL_FIELDS:
        raise ConfigError(f"appliance_pool may name only {sorted(POOL_FIELDS)}")
    if pool:
        if data["runtime"] != "macos-appliance" or not guest or appliance:
            raise ConfigError("appliance_pool requires a macOS guest connection and excludes appliance")
        if not tools.get("domain"):
            raise ConfigError("appliance_pool requires explicit guest tools.domain")
        from .runtimes.macos_pool import PINNED_IMAGE
        if not isinstance(pool.get("image"), str) or not PINNED_IMAGE.fullmatch(pool["image"]):
            raise ConfigError("appliance_pool.image must be pinned by sha256")
        for key in ("base_disk", "base_system", "data_root"):
            path = pool.get(key)
            if not isinstance(path, str) or not path.startswith("/") or any(c in path for c in (",", "\n", "\r")):
                raise ConfigError(f"appliance_pool.{key} must be an absolute Linux path without commas")
            if key != "data_root" and not exists(path):
                raise ConfigError(f"appliance_pool.{key} does not exist")
        if "nvram_seed" in pool:
            path = pool["nvram_seed"]
            if (not isinstance(path, str) or not path.startswith("/")
                    or any(c in path for c in (",", "\n", "\r")) or not exists(path)):
                raise ConfigError("appliance_pool.nvram_seed must be an existing absolute Linux path without commas")
        templates = pool.get("templates")
        if (not isinstance(templates, list) or not templates or any(
                not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", t)
                for t in templates)):
            raise ConfigError("appliance_pool.templates must name verified installed templates")
        if pool.get("base_guests_disabled") is not True:
            raise ConfigError("appliance_pool.base_guests_disabled must attest the prepared base")
        for key, default, low, high in (("ssh_port_base", 51000, 1024, 65535),
                                       ("boot_timeout", 600, 10, 900),
                                       ("shutdown_timeout", 180, 10, 900),
                                       ("image_uid", 1000, 0, 2 ** 31 - 1),
                                       ("image_gid", 1000, 0, 2 ** 31 - 1)):
            value = pool.get(key, default)
            if type(value) is not int or not low <= value <= high:
                raise ConfigError(f"appliance_pool.{key} must be {low}..{high}")
        for key in ("docker", "qemu_img"):
            if key in pool and (not isinstance(pool[key], str) or not pool[key]):
                raise ConfigError(f"appliance_pool.{key} must be an executable path")
        capacity = dict({"max_runners": 2, "memory_bytes": 24 * 1024 ** 3}, **capacity)

    storage = data.get("storage") or {}
    if not isinstance(storage, dict) or set(storage) - {"root", "default_bytes", "readonly_root"}:
        raise ConfigError("storage may name only root, default_bytes and readonly_root")
    if storage:
        if data["runtime"] != "linux-container":
            raise ConfigError("storage is supported only by linux-container")
        path = storage.get("root")
        if not isinstance(path, str) or not path.startswith("/") or path == "/" or any(c in path for c in (",", "\n", "\r")):
            raise ConfigError("storage.root must be a dedicated absolute Linux path")
        size = storage.get("default_bytes", 100 * 1024 ** 3)
        if type(size) is not int or not 64 * 1024 ** 2 <= size <= 2 ** 63 - 1:
            raise ConfigError("storage.default_bytes must be at least 64 MiB and fit a signed 64-bit size")
        readonly_root = storage.get("readonly_root", True)
        if type(readonly_root) is not bool:
            raise ConfigError("storage.readonly_root must explicitly be true or false")
        storage = dict(storage, default_bytes=size, readonly_root=readonly_root)

    windows_storage = data.get("windows_storage") or {}
    if not isinstance(windows_storage, dict) or set(windows_storage) - {"enabled", "root", "default_limit", "reserve_bytes"}:
        raise ConfigError("windows_storage may name only enabled, root, default_limit and reserve_bytes")
    if windows_storage:
        if data["runtime"] != "windows-process":
            raise ConfigError("windows_storage is supported only by windows-process")
        if type(windows_storage.get("enabled")) is not bool:
            raise ConfigError("windows_storage.enabled must explicitly be true or false")
        path = windows_storage.get("root")
        if (not isinstance(path, str) or not ntpath.isabs(path) or not ntpath.splitdrive(path)[0]
                or path.startswith(("\\\\", "//")) or not ntpath.basename(path.rstrip("/\\"))
                or any(c in path for c in ("\n", "\r"))):
            raise ConfigError("windows_storage.root must be a dedicated absolute local Windows path")
        limit, reserve = windows_storage.get("default_limit", 100 * 1024 ** 3), windows_storage.get("reserve_bytes", 20 * 1024 ** 3)
        if (type(limit) is not int or limit < 1024 ** 3 or limit % (1024 ** 2)
                or type(reserve) is not int or reserve < 0):
            raise ConfigError("windows_storage default_limit must be whole MiB and at least 1 GiB; reserve_bytes nonnegative")
        windows_storage = dict(windows_storage, default_limit=limit, reserve_bytes=reserve)

    return Config(host_id=host_id, runtime=data["runtime"],
                  listen=_listen(data["listen"]), controller=controller,
                  cert=str(tls["cert"]), key=str(tls["key"]),
                  ca=str(tls["ca"]), permitted=permitted, tools=dict(tools),
                  version=str(data.get("version") or "0"),
                  capacity=dict(capacity), guest=guest, appliance=appliance,
                  appliance_pool=dict(pool), storage=dict(storage), windows_storage=dict(windows_storage))


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as e:
        raise ConfigError(f"cannot read {path}: {e.strerror or e}")
    except ValueError as e:
        raise ConfigError(f"{path} is not JSON: {e}")
    return parse(data)
