"""Windows runners as process trees on a Windows Server worker.

Design 9.2 and 10.5: no Windows containers. Each runner is a **service**, its
processes held in a **Job Object** that caps memory and CPU, running under
**its own account**, over **its own directory tree** that nothing else on the
worker can read. The same five areas as on Linux (design 15.1), minus the
nested engine, which Windows runners do not have: `D:\\runners\\<runner_id>\\`
`work`, `cache`, `reg` and `logs`, plus `tmp` for the runner's TEMP.

**The account is the service's own virtual account**, `NT SERVICE\\rnr-<id>`,
not a local user the runtime creates. It exists exactly as long as the
service, has no password to generate, store or pass on a command line, and
cannot log on interactively. Its SID is derived from the service name
(`service_sid`), but Windows maps that SID to an account only once the service
exists: `icacls` answers "No mapping between account names and security IDs was
done" for a service that is not there yet. So `create` makes the directories,
then the service, then locks the tree to it, and starts the service last -
nothing of this runner's runs while its tree is still open.

**The service runs `agent.jobhost`**, through NSSM, the wrapper this host
already runs its Windows runner under. The job host creates the Job Object,
enters it, and starts the runner's `run.cmd`, which inherits the membership.
Limits and environment reach it in `reg\\unit.json`, not on a command line.

**The runner's software is a template**, a directory named by the spec's
`image`, copied into `reg` at create: `run.cmd`, which starts the runner once
it is registered; `register.ps1`, which reads the registration plan as JSON on
standard input and answers `{"registration_id", "registration_uuid"}`; and
`deregister.ps1`. The same two fixed entry points as the Linux image, so this
module never learns which forge a runner serves.

**Every step of `create` is safe to repeat**, so a create the agent died in
the middle of converges when it is driven again (T-0308). The service is
configured on every call; the template is copied once, recorded by a marker.

The argv is exercised against a fake host, and the Job Object in
`agent/jobhost.py` against this machine's real kernel. It first ran against a
real worker - this host - on 2026-09-19, which is where the two corrections
above come from.
"""
import hashlib
import json
import ntpath
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

from .. import cpu, hardware, naming
from ..jobs import current_job
from ..windows_timeouts import registration_limits
from .localfs import LocalFs
from .windows_storage import UNMANAGED, WindowsStorage

#: Areas created per runner. `docker` is None on Windows: no nested engine.
AREAS = tuple(a for a in naming.AREAS if a != "docker")

#: What a `recreate` keeps. The same rule as Linux (design 15.1): the cache
#: and the logs stay; the workspace, the registration and temp go.
KEPT_ON_RECREATE = ("cache", "logs")

#: Told to the runner, so it knows the layout without being built for it.
LAYOUT_ENV_KEYS = {"work": "RUNNER_WORK_DIR", "cache": "RUNNER_CACHE_DIR",
                   "reg": "RUNNER_REG_DIR", "logs": "RUNNER_LOG_DIR"}

TEMPLATE_FILES = ("run.cmd", "register.ps1", "deregister.ps1")

#: GitHub's job hooks (#7): each variable and the script it names, in the
#: runner's reg directory under `hooks\`. Every create copies the agent's own
#: copy there, `run_hook.js`, `runner_disk.py` and `runner_guard.py` (the
#: Linux unit's origin check, byte for byte) beside them, so a
#: redeployed agent and a recreated runner are all a change to them needs -
#: and a deploy, which swaps the agent's folder away, never leaves a job
#: without its hook. GitHub runs a hook only when its path ends in .js, .sh or
#: .ps1. Forgejo has no hooks.
HOOK_SOURCE = Path(__file__).resolve().parents[1] / "hooks" / "windows"
HOOK_SCRIPTS = {"ACTIONS_RUNNER_HOOK_JOB_STARTED": "job-started.js",
                "ACTIONS_RUNNER_HOOK_JOB_COMPLETED": "job-completed.js"}
HOOK_FILES = ("run_hook.js", "runner_disk.py", "runner_guard.py", *HOOK_SCRIPTS.values())

#: The file that asks the job host to drain the runner (see agent/jobhost.py).
DRAIN_REQUEST = "drain.request"
TEMPLATE_MARKER = ".template"

#: Which of the runner's own directories each clearable scope is (T-1601).
#: The scopes this runtime offers are generated from this table. No engine
#: scopes: there is no engine, so nothing of the runner's would hold one.
SCOPE_AREAS = {"workspace": "work", "toolcache": "cache", "temp": "tmp"}
SUPPORTED_SCOPES = frozenset(SCOPE_AREAS)
DEFAULT_SCOPES = ("toolcache", "temp")

STOP_TIMEOUT = 60
TELEMETRY_STALE = 60

#: Well-known SIDs for the directory ACL: LocalSystem and Administrators.
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"

TOOLS = {
    "nssm": r"C:\Program Files\nssm\nssm.exe",
    "sc": r"C:\Windows\System32\sc.exe",
    "icacls": r"C:\Windows\System32\icacls.exe",
    "powershell": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
    "python": sys.executable,
    "templates": r"C:\ProgramData\nomercy\templates",
}

_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3,
          "t": 1024 ** 4}


def service_sid(service_name):
    """The SID Windows gives a service's virtual account: S-1-5-80 followed by
    the SHA-1 of the upper-cased name in UTF-16LE, as five little-endian
    words. Checked against `sc.exe showsid` in the tests."""
    digest = hashlib.sha1(service_name.upper().encode("utf-16-le")).digest()
    return "S-1-5-80-" + "-".join(str(w) for w in struct.unpack("<5I",
                                                                 digest))


def size_bytes(text):
    """Docker's size syntax ("32g", "512m"), 1024-based as Docker's is."""
    text = str(text).strip().lower().rstrip("b") if text else ""
    if not text:
        return None
    unit = text[-1] if text[-1] in "kmgt" else ""
    number = text[:-1] if unit else text
    return int(float(number) * _UNITS[unit]) or None


def _exec(args, input=None, timeout=30):
    """Run a program with a literal argument list. Returns (ok, out, err).
    Never a shell and never a string (T-0401)."""
    try:
        p = subprocess.run([*args], input=input, text=True,
                           capture_output=True, timeout=timeout)
        return p.returncode == 0, p.stdout.strip(), p.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "", f"timed out after {timeout}s"
    except OSError as e:
        return False, "", str(e)


class WindowsProcessRuntime:
    kind = "windows-process"

    def __init__(self, run=None, fs=None, tools=None, storage=None, storage_backend=None):
        self._run = run or _exec
        self._fs = fs or LocalFs()
        self._tools = dict(TOOLS, **(tools or {}))
        self._storage = storage_backend
        if self._storage is None and (storage or {}).get("enabled"):
            self._storage = WindowsStorage(storage, self._run, self._tools["powershell"])

    # ---- where things are ----------------------------------------------------

    def paths(self, runner_id):
        """Every path this runner owns, derived from its runner_id alone."""
        rid = naming.check(runner_id)
        areas = naming.names(rid, "windows")
        root = ntpath.dirname(areas["work"])
        out = {area: areas[area] for area in AREAS}
        out["root"] = root
        out["tmp"] = ntpath.join(root, "tmp")
        return out

    def scope_locations(self, runner_id):
        """Each scope's directory: always inside this runner's own tree."""
        p = self.paths(runner_id)
        return {scope: p[area] for scope, area in SCOPE_AREAS.items()}

    def _nssm(self, *args, timeout=60):
        """NSSM, with its answer made readable. It writes UTF-16, so read as
        text its output carries a NUL between every character: "Can't open
        service!" matched none of the strings this module looks for. A
        removal of a service that was never made then failed instead of being
        the no-op it is, and every status read came back unknown. Found on
        the first live Windows worker, 2026-09-19."""
        ok, out, err = self._run([self._tools["nssm"], *args], timeout=timeout)
        return ok, _readable(out), _readable(err)

    def _sc(self, *args, timeout=30):
        return self._run([self._tools["sc"], *args], timeout=timeout)

    # ---- lifecycle -----------------------------------------------------------

    def _service_command(self, runner_id, action):
        result = self._nssm(action, naming.unit_name(runner_id),
                            timeout=STOP_TIMEOUT + 30)
        if result[0]:
            return
        pending = {"start": "starting", "stop": "stopping"}[action]
        marker = f"SERVICE_{action.upper()}_PENDING"
        if marker not in result[1] + result[2]:
            self._check(result)
        # NSSM can return a failure while SCM is still completing the
        # requested transition, especially under ARM emulation. Confirm the
        # final state before treating the operation as failed or complete.
        deadline = time.monotonic() + STOP_TIMEOUT + 30
        while True:
            state = self.status(runner_id)
            if state.get("exists") is True and state.get("running") is (action == "start"):
                return
            if state.get("state") != pending:
                raise RuntimeError(f"service {action} was not confirmed: {state.get('state')}")
            if time.monotonic() >= deadline:
                raise RuntimeError(f"service {action} is still {pending}; final state was not confirmed")
            time.sleep(1)

    def create(self, runner_id, spec):
        rid = naming.check(runner_id)
        name = naming.unit_name(rid)
        image = (spec or {}).get("image")
        if not image:
            raise ValueError("a unit needs a template")
        if (spec or {}).get("disk_limit") and not self._storage:
            raise ValueError("disk_limit requires configured Windows storage")
        template = ntpath.join(self._tools["templates"], image)
        if not self._fs.exists(template):
            raise RuntimeError(f"no runner template {image!r} on this worker")
        p = self.paths(rid)

        # Attach/prove the owned filesystem before creating any directory.
        # Existing plain trees require explicit offline migration.
        disk = None
        if self._storage:
            disk = self._storage.ensure(rid, (spec or {}).get("disk_limit"))

        # 1. The directories.
        self._fs.makedirs(p["root"])
        for key in (*AREAS, "tmp"):
            self._fs.makedirs(p[key])

        # 2. The service, before the tree is locked to it. Its virtual
        #    account's SID is derived from the name, but Windows maps that SID
        #    to an account only once the service exists: icacls answers "No
        #    mapping between account names and security IDs was done" for a
        #    service that is not there yet, which is how the first live
        #    Windows worker failed (2026-09-19). So the service is made first
        #    and started last, and nothing runs while the tree is open.
        service = self.status(rid)
        if self._storage and service.get("exists") is None:
            raise RuntimeError("service state is unknown; create is held")
        if service["exists"] is not True:
            parameters = self._jobhost_args(rid, spec, p, disk)
            ok, out, err = self._nssm(
                "install", name, self._tools["python"], *parameters)
            if not ok and "exists" not in (out + err):
                raise RuntimeError(err or out or "nssm install failed")

        # 3. Storage, locked to this runner before anything is in it.
        self._acl(p["root"], name)
        if self._tools.get("short_workspaces"):
            from ..windows_workspace import ensure_alias
            ensure_alias(self._tools["short_workspaces"], rid, p["work"],
                         self._run, self._tools["icacls"], self._tools["powershell"])
        # Configure the account/startup policy before creating executable
        # runner files: a host reboot during create must not launch a ready
        # unit under NSSM's initial LocalSystem account.
        self._configure(name, p, spec)

        # 4. The runner's software, once. A re-driven create must not copy
        #    over a registered runner's files while its service holds them.
        #    Until it is registered nothing holds them, so a copy a failed undo
        #    half deleted - the marker kept, the software gone - is made whole.
        marker = ntpath.join(p["reg"], TEMPLATE_MARKER)
        if not self._fs.exists(marker) or (
                not self._registered(p["reg"])
                and self._incomplete_copy(template, p["reg"])):
            self._fs.copytree(template, p["reg"])
            self._fs.write_text(marker, image)

        # 4b. GitHub's job hooks, before the unit file that points at them,
        #     and on every create, so a redeployed agent's copy is the one used.
        if _serves_github(spec):
            hooks = ntpath.join(p["reg"], "hooks")
            self._fs.makedirs(hooks)
            for hook in HOOK_FILES:
                self._fs.write_text(ntpath.join(hooks, hook),
                                    (HOOK_SOURCE / hook).read_text(encoding="utf-8"))

        # 5. What the job host reads: limits and environment, never argv.
        self._fs.write_text(ntpath.join(p["reg"], "unit.json"),
                            json.dumps(self._unit(rid, spec, p)))

        # 5b. Out of a job's reach: a job runs as this service's account,
        #     which may change anything else in its tree. The hooks, and the
        #     unit file that names them and RUNNER_TRUSTED_AUTHORS, deny it
        #     every kind of write; LocalSystem, the agent, is not denied.
        self._lock_from_jobs(name, p, _serves_github(spec))

        running = self.status(rid).get("running")
        if running is None:
            raise RuntimeError("service state is unknown; start is held")
        if running is False:
            self._service_command(rid, "start")
        return name

    def _unit(self, rid, spec, p):
        env = dict(spec.get("env") or {})
        for area, key in LAYOUT_ENV_KEYS.items():
            env[key] = p[area]
        env["TEMP"] = env["TMP"] = p["tmp"]
        # These profiles belong to the runner for both storage backends.
        # Plain directory workers (including QEMU ARM guests) must not put
        # job caches in a service profile outside clear_cache's scopes.
        env["HOME"] = env["USERPROFILE"] = p["work"]
        env["APPDATA"] = env["LOCALAPPDATA"] = p["cache"]
        # In particular, the machine's CARGO_HOME holds the installed shims;
        # jobs need their own writable Cargo cache. RUSTUP_HOME still points
        # to the shared, preinstalled compiler toolchains.
        for variable, directory in {
            "CARGO_HOME": "cargo", "DOTNET_CLI_HOME": "dotnet",
            "NUGET_PACKAGES": "nuget", "NUGET_HTTP_CACHE_PATH": "nuget-http",
            "NUGET_SCRATCH": "nuget-scratch", "GRADLE_USER_HOME": "gradle",
            "NPM_CONFIG_CACHE": "npm", "PIP_CACHE_DIR": "pip",
            "GOCACHE": "go-build", "GOMODCACHE": "go-mod",
        }.items():
            env[variable] = ntpath.join(p["cache"], directory)
        if self._tools.get("short_workspaces"):
            from ..windows_workspace import alias_path
            env["RUNNER_JOB_WORK_DIR"] = alias_path(self._tools["short_workspaces"], rid)
        if _serves_github(spec):
            hooks = ntpath.join(p["reg"], "hooks")
            for key, hook in HOOK_SCRIPTS.items():
                env[key] = ntpath.join(hooks, hook)
            # The .js hooks hand their work to the Python the job host runs
            # under, which the runner's account can already execute and a
            # code deploy does not move.
            env["RUNNER_HOOK_PYTHON"] = self._tools["python"]
        cpus = str(spec.get("cpus") or "").strip()
        return {"runner_id": rid, "env": env,
                "memory_bytes": size_bytes(spec.get("memory")),
                "cpus": float(cpus) if cpus not in ("", "0") else None,
                "cpuset": spec.get("cpuset") or None}

    #: Every kind of write, and delete, rename, re-permission and take-over:
    #: what a job is denied on the files its own hook and job host run from.
    DENY_WRITE = "DE,WD,AD,WEA,WA,WDAC,WO"

    def _lock_from_jobs(self, name, p, hooks):
        sid = service_sid(name)
        if hooks:
            self._check(self._run([
                self._tools["icacls"], ntpath.join(p["reg"], "hooks"), "/deny",
                f"*{sid}:(OI)(CI)({self.DENY_WRITE.replace('WA,', 'WA,DC,')})", "/Q"],
                timeout=120))
        self._check(self._run([
            self._tools["icacls"], ntpath.join(p["reg"], "unit.json"), "/deny",
            f"*{sid}:({self.DENY_WRITE})", "/Q"], timeout=120))

    def _acl(self, root, name):
        """Only LocalSystem, Administrators and this runner's own account, and
        nothing inherited from above: another runner's account is not in the
        list, so it cannot read this tree."""
        self._check(self._run([
            self._tools["icacls"], root, "/inheritance:r", "/grant:r",
            f"*{SYSTEM_SID}:(OI)(CI)F", f"*{ADMINISTRATORS_SID}:(OI)(CI)F",
            f"*{service_sid(name)}:(OI)(CI)M", "/Q"], timeout=120))

    def _jobhost_args(self, rid, spec, p, disk=None):
        unit = self._unit(rid, spec, p)
        limits = {key: unit[key] for key in ("memory_bytes", "cpus", "cpuset")}
        from ..jobhost import checked_limits
        checked_limits(limits)
        launcher = str(Path(__file__).resolve().parents[1] / "launch_jobhost.py")
        args = ["-I", "-B", launcher, "--root", p["root"],
                "--limits-json", json.dumps(limits, separators=(",", ":")),
                "--runner-id", rid]
        if disk:
            args.extend(["--volume-guid", disk["volume_guid"]])
        return args

    def _registered(self, reg):
        """A registration file in the place either forge's template keeps it:
        the GitHub runner's beside its binaries, Forgejo's beside the
        entry points."""
        return any(self._fs.exists(ntpath.join(reg, *where))
                   for where in ((".runner",), ("agent", ".runner")))

    def _incomplete_copy(self, template, reg):
        """Something the template has at its top level is missing here, or a
        directory that is full there is empty here."""
        for name in self._fs.listdir(template):
            here = ntpath.join(reg, name)
            if not self._fs.exists(here):
                return True
            if self._fs.listdir(ntpath.join(template, name)) and not self._fs.listdir(here):
                return True
        return False

    def _configure(self, name, p, spec):
        self._check(self._sc("config", name, "obj=", f"NT SERVICE\\{name}",
                             "start=", "demand" if self._storage else "auto"))
        self._check(self._sc("sidtype", name, "unrestricted"))
        rid = name[len(naming.PREFIX) + 1:]
        self._registration_key(rid, "ensure")
        disk = self._storage.verify(rid) if self._storage else None
        # Protected SCM parameters supply resource limits and volume identity.
        # A job can edit unit.json but cannot raise these ceilings on restart.
        parameters = subprocess.list2cmdline(self._jobhost_args(rid, spec, p, disk))
        self._check(self._nssm("set", name, "AppParameters", parameters))
        log = ntpath.join(p["logs"], "runner.log")
        for setting in (("AppDirectory", p["work"]),
                        ("AppStdout", log), ("AppStderr", log),
                        # Ctrl+C first, and the grace a deregistration needs.
                        ("AppStopMethodConsole", str(STOP_TIMEOUT * 1000)),
                        ("AppExit", "Default", "Restart")):
            self._check(self._nssm("set", name, *setting))

    def _registration_key(self, rid, action):
        script = str(Path(__file__).resolve().parents[1] / "registration_keys.ps1")
        self._check(self._run([self._tools["powershell"], "-NoProfile", "-NonInteractive",
                              "-ExecutionPolicy", "Bypass", "-File", script,
                              "-RunnerId", naming.check(rid), "-Action", action],
                              timeout=registration_limits()["key_setup"]))

    def start(self, runner_id):
        """Started, and in service. What a drain leaves behind is undone
        first - the request file the job host would answer with Ctrl+Break,
        and NSSM's leave-it-down on exit - so a service started after a drain
        serves instead of exiting again at once. A running service is left
        running: NSSM refuses to start one twice."""
        name = naming.unit_name(runner_id)
        p = self.paths(runner_id)
        if self._storage:
            self._storage.mount(runner_id)
        self._fs.remove(ntpath.join(p["reg"], DRAIN_REQUEST))
        self._check(self._nssm("set", name, "AppExit", "Default", "Restart"))
        self._check(self._sc("config", name, "start=",
                             "demand" if self._storage else "auto"))
        running = self.status(runner_id).get("running")
        if running is None:
            raise RuntimeError("service state is unknown; start is held")
        if running is False:
            self._service_command(runner_id, "start")

    def stop(self, runner_id):
        """Ctrl+C to the runner, then the grace a deregistration needs. A
        service that is already down - a drained runner's, say - is left as
        it is: NSSM refuses to stop one that has not been started, and a stop
        is asked for to make the unit down, which it is."""
        state = self.status(runner_id)
        if state.get("exists") is False:
            return
        if state.get("exists") is None:
            raise RuntimeError("service state is unknown; stop was not confirmed")
        if state.get("running") is None:
            raise RuntimeError("service state is unknown; stop was not confirmed")
        self._check(self._sc("config", naming.unit_name(runner_id),
                             "start=", "demand"))
        if not state.get("running"):
            return
        self._service_command(runner_id, "stop")

    def restart(self, runner_id):
        self.stop(runner_id)
        self.start(runner_id)

    def drain(self, runner_id):
        """A graceful stop that stays stopped (OPEN-7). NSSM is told not to
        restart the service when its program exits, and the job host is
        asked - by a file in the runner's own tree, which only this runner's
        account and the agent can write - to pass the runner a Ctrl+Break.
        The runner takes nothing new, finishes what it has, exits, and the
        service stays down. Nothing here waits for it or kills anything.

        Asked again on every reconciler pass until the runner is drained;
        the job host passes the Ctrl+Break on once, however often the file
        is written."""
        name = naming.unit_name(runner_id)
        p = self.paths(runner_id)
        if self._storage:
            self._storage.verify(runner_id)
        self._check(self._sc("config", name, "start=", "demand"))
        self._check(self._nssm("set", name, "AppExit", "Default", "Exit"))
        self._fs.write_text(ntpath.join(p["reg"], DRAIN_REQUEST), "drain")

    def cancel_drain(self, runner_id):
        """Back into service once drained: restarts on exit again, and the
        service started - which is what a start does."""
        self.start(runner_id)

    def remove(self, runner_id, keep_data):
        """Remove the service and its storage. Safe when any of it is absent."""
        rid = naming.check(runner_id)
        name = naming.unit_name(rid)
        if self._storage:
            state = self.status(rid)
            if state.get("exists") is None or state.get("running") is None:
                raise RuntimeError("service state is unknown; storage is retained")
            if state.get("exists"):
                self.stop(rid)
                if self.status(rid).get("running") is not False:
                    raise RuntimeError("service has not stopped; storage is retained")
            # No writes through an unmounted directory, including compensation.
            if keep_data:
                self._storage.mount(rid)
        current = self.status(rid)
        if self._storage and (current.get("exists") is None or current.get("running") is not False):
            raise RuntimeError("service quiescence changed; removal is held")
        if current["exists"] is not False:
            # Stopped first, so the runner gets its grace; a service that is
            # already stopped makes this fail, which changes nothing.
            self._nssm("stop", name, timeout=STOP_TIMEOUT + 30)
            ok, out, err = self._nssm("remove", name, "confirm")
            if not ok and not _absent(out + err):
                raise RuntimeError(err or out or "nssm remove failed")
        p = self.paths(rid)
        if not keep_data and self._tools.get("short_workspaces"):
            from ..windows_workspace import remove_alias
            remove_alias(self._tools["short_workspaces"], rid, p["work"])
        if self._storage and not keep_data:
            self._storage.remove(rid)
            self._registration_key(rid, "remove")
            return
        if self._storage:
            # Compensation must keep the prior workspace as well as caches
            # and logs. Registration and temp are reset for fresh enrollment.
            doomed = [p["reg"], p["tmp"]]
        else:
            doomed = ([p[a] for a in AREAS if a not in KEPT_ON_RECREATE]
                      + [p["tmp"]]) if keep_data else [p["root"]]
        left = []
        for path in doomed:
            try:
                self._fs.rmtree(path)
            except OSError as e:
                left.append(f"{ntpath.basename(path)}: {e}")
        if left:
            raise RuntimeError("storage left behind: " + "; ".join(left))
        if not keep_data:
            self._registration_key(rid, "remove")

    def _check(self, result):
        ok, out, err = result
        if not ok:
            raise RuntimeError(err or out or "failed")

    # ---- observation ---------------------------------------------------------

    def status(self, runner_id):
        """Whether the service exists and runs. `exists` is None - unknown -
        when the service manager could not be asked."""
        ok, out, err = self._nssm("status", naming.unit_name(runner_id),
                                  timeout=30)
        text = out + err
        if not ok:
            if _absent(text):
                return {"exists": False, "running": False, "state": "absent"}
            return {"exists": None, "running": None, "state": "unknown"}
        state = text.strip().split()[-1] if text.strip() else ""
        running = True if state == "SERVICE_RUNNING" else (
            False if state == "SERVICE_STOPPED" else None)
        result = {"exists": True, "running": running,
                "state": {"SERVICE_RUNNING": "running",
                          "SERVICE_STOPPED": "exited",
                          "SERVICE_PAUSED": "paused",
                          "SERVICE_START_PENDING": "starting",
                          "SERVICE_STOP_PENDING": "stopping"}.get(state,
                                                                  "unknown")}
        if self._storage:
            try:
                self._storage.verify(runner_id)
                result["storage_ready"] = True
            except (OSError, RuntimeError, ValueError) as exc:
                result.update(storage_ready=False, storage_error=str(exc))
                if running is not False:
                    result.update(running=None, state="unknown")
        return result

    def telemetry(self, runner_id):
        """What the job host last measured, if it is recent. A stale or
        missing report is unknown, not zero.

        Disk figures are not measured here. This is called every light beat
        (agent/heartbeat.py's `_telemetry`, every ten seconds), and verifying
        the owned filesystem means the storage helper - a PowerShell process
        that can wait up to 8s for the host-wide storage lock. With two
        runners that pushed a beat's measurement to 26-39s, past the
        dashboard's 30s freshness window (2026-09-22). A runner's disk is
        measured instead by `probe`, which only the deep probe calls, about
        once every thirty beats - a runner whose disk is not checked here
        reads as unknown, never as zero."""
        result = {"cpu_percent": None, "mem_used_bytes": None,
                  "mem_limit_bytes": None, "cpu_cores": None,
                  "host_cores": os.cpu_count()}
        p = self.paths(runner_id)
        if self._storage:
            result.update(disk_limit_enforced=None, disk_limit_bytes=None,
                          disk_virtual_bytes=None, disk_free_bytes=None,
                          disk_used_bytes=None)
        try:
            unit = json.loads(self._fs.read_text(
                ntpath.join(p["reg"], "unit.json")))
            result["mem_limit_bytes"] = unit.get("memory_bytes")
            result["cpu_cores"] = cpu.ceiling(unit.get("cpuset"),
                                             float(unit.get("cpus") or 0) * 1e9)
        except (OSError, ValueError):
            pass
        path = ntpath.join(p["logs"], "telemetry.json")
        try:
            report = json.loads(self._fs.read_text(path))
        except (OSError, ValueError):
            return result
        if time.time() - float(report.get("at") or 0) > TELEMETRY_STALE:
            return result
        result["cpu_percent"] = report.get("cpu_percent")
        result["mem_used_bytes"] = report.get("mem_used_bytes")
        return result

    def jobs(self, runner_ids):
        """Which job each running unit says it has, from the log directly -
        never through `logs()`, which verifies the runner's volume through
        the storage helper. This runs on every light beat
        (agent/heartbeat.py's `_jobs`); with two runners that verify pushed a
        beat's measurement well past the dashboard's 30s freshness window,
        the same failure Task 18 fixed for `telemetry()` (2026-09-22)."""
        return {rid: current_job(self._tail_log(rid, 86400)) for rid in runner_ids}

    def logs(self, runner_id, since_seconds, max_bytes=256 * 1024):
        """The tail of the runner's own output, for an operator's log
        request - this keeps verifying the runner's volume through the
        storage helper."""
        if self._storage:
            self._storage.verify(runner_id)
        return self._tail_log(runner_id, since_seconds, max_bytes)

    def _tail_log(self, runner_id, since_seconds, max_bytes=256 * 1024):
        """The tail of the runner's own output. The file carries no
        timestamps, so `since_seconds` decides only whether it has been
        written to at all in that window."""
        path = ntpath.join(self.paths(runner_id)["logs"], "runner.log")
        try:
            if time.time() - self._fs.mtime(path) > since_seconds:
                return ""
            return self._fs.tail(path, max_bytes)
        except OSError:
            return ""

    def _owned_disk(self, runner_id):
        """The runner's own disk as the storage helper verified it, or None
        when it has none: no storage on this worker, or a plain directory
        the helper has no manifest for (a runner made before its worker had
        owned storage). Any other failure to verify is raised - a disk that
        is there but wrong is not measured as if it were not."""
        if not self._storage:
            return None
        try:
            return self._storage.verify(runner_id)
        except RuntimeError as exc:
            if UNMANAGED in str(exc):
                return None
            raise

    def _volume(self, path, disk):
        """The volume `path` is on: the runner's own disk when it has one,
        else whatever the worker's filesystem says holds it."""
        if disk is not None:
            used = disk["capacity_bytes"] - disk["free_bytes"]
            return {"volume_used_bytes": used,
                    "volume_total_bytes": disk["capacity_bytes"]}
        usage = self._fs.disk_usage(path) or {}
        return {"volume_used_bytes": usage.get("used_bytes"),
                "volume_total_bytes": usage.get("total_bytes")}

    def probe(self, runner_id, probe):
        disk = self._owned_disk(runner_id)
        if disk is not None and probe == "disk_usage":
            return {"ok": True, "value": disk["capacity_bytes"] - disk["free_bytes"],
                    "total_bytes": disk["capacity_bytes"]}
        p = self.paths(runner_id)
        volume = {}
        if probe == "disk_usage":
            value = self._fs.du(p["root"])
            volume = self._volume(p["root"], disk)
        elif probe == "cache_size":
            value = self._fs.du(p["cache"])
            volume = self._volume(p["cache"], disk)
            if disk is not None:
                # No cap, but on the runner's own disk: that disk, its alone,
                # is what bounds the cache.
                volume["total_bytes"] = disk["capacity_bytes"]
        elif probe == "agent_version":
            try:
                value = self._fs.read_text(
                    ntpath.join(p["reg"], "agent_version")).strip()
            except OSError as e:
                return {"ok": False, "value": None, "error": str(e)}
        elif probe == "job_state":
            return {"ok": False,
                    "error": "job state is authoritative at the forge"}
        else:
            return {"ok": False, "error": "unknown probe"}
        if value is None:
            return {"ok": False, "error": "could not be measured"}
        return {"ok": True, "value": value, **volume}

    def instances(self):
        """Every runner service on this worker, and its state. Raises when
        the service manager cannot be asked: an empty list would say this
        worker runs nothing."""
        ok, out, err = self._sc("query", "type=", "service", "state=", "all",
                                timeout=60)
        if not ok:
            raise RuntimeError(err or out or "sc query failed")
        found, current = [], None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("SERVICE_NAME:"):
                current = line.split(":", 1)[1].strip()
                continue
            if current and line.startswith("STATE") and \
                    current.startswith(naming.PREFIX + "-"):
                rid = current[len(naming.PREFIX) + 1:]
                try:
                    naming.check(rid)
                except naming.InvalidRunnerId:
                    current = None
                    continue
                state = line.split()[-1]
                found.append({"runner_id": rid,
                              "state": {"RUNNING": "running",
                                        "STOPPED": "stopped"}.get(state,
                                                                 "unknown")})
                current = None
        return found

    def origin_guard_report(self, runner_id):
        """Whether this runner's job-started hook refuses outside code
        (agent/origin_guard.py): every file in the hooks directory its
        unit.json names must still be this agent's own copy, and its log
        directory holds the hook's record of its last run. None for a runner
        that is not GitHub's (no hook in its unit.json) or cannot be read."""
        from ..origin_guard import LAST_RESULT, measure_tree
        rid = naming.check(runner_id)
        p = self.paths(rid)
        try:
            unit = json.loads(self._fs.read_text(ntpath.join(p["reg"], "unit.json")))
            hook = ((unit or {}).get("env") or {}).get("ACTIONS_RUNNER_HOOK_JOB_STARTED")
            if not hook:
                return None
            expected = {name: (HOOK_SOURCE / name).read_text(encoding="utf-8")
                        for name in HOOK_FILES}
            return measure_tree(self._fs, ntpath.dirname(hook), expected, ntpath.join,
                                "runner_guard.py", "GUARD_VERSION = ",
                                ntpath.join(p["logs"], LAST_RESULT))
        except (OSError, ValueError, AttributeError):
            return None

    # ---- cache ---------------------------------------------------------------

    def clear_cache(self, runner_id, policy):
        """Clear the named scopes, each one a directory this runner owns.

        A scope this runtime cannot clear is reported as an error for that
        scope, never skipped in silence. A failing scope does not stop the
        others. A second call finds nothing to free and still succeeds.
        """
        where = self.scope_locations(runner_id)
        if self._storage:
            self._storage.verify(runner_id)
        scopes = list((policy or {}).get("scopes") or DEFAULT_SCOPES)
        per_scope, errors, measured = {}, {}, True
        for scope in scopes:
            if scope not in SUPPORTED_SCOPES:
                errors[scope] = "not supported by this runtime"
                continue
            before = self._fs.du(where[scope])
            try:
                self._fs.clear_dir(where[scope])
            except OSError as e:
                errors[scope] = str(e)[:200]
            after = self._fs.du(where[scope])
            if before is None or after is None:
                measured = False
                continue
            per_scope[scope] = max(0, before - after)
        return {"per_scope": per_scope, "errors": errors,
                "total_bytes": sum(per_scope.values()),
                "measured": measured}

    # ---- what this runtime can do --------------------------------------------


    def _templates(self):
        """The templates on this worker, which are the only units it can
        make. A directory that cannot be read is no templates rather than a
        crash: a worker must keep answering."""
        try:
            return sorted(self._fs.listdir(self._tools["templates"]))
        except OSError:
            return []

    def capabilities(self):
        return {"kind": self.kind,
                # What a unit here is made from, and which ones exist: a
                # cell whose template is not on any worker cannot be built,
                # and saying so is cheaper than failing at create.
                "builds_from": "template",
                "templates": self._templates(),
                # GitHub runs job containers on Linux only; Windows runners
                # here run on the OS (design 9.2).
                "job_containers": False,
                "nested_builds": False,
                "resettable_os": False,
                "disk_limit_enforced": self._storage is not None,
                "disk_quota": self._storage is not None,
                # OPEN-7: a graceful stop that stays stopped.
                "supports_drain": True,
                "clear_cache": True,
                "cache_scopes": sorted(SUPPORTED_SCOPES),
                # What a pinned window is cut from: the controller stages
                # each runner's cpuset over these, so it needs the number
                # before any runner is here to report it - the same key
                # agent/runtimes/linux_container.py declares, the same way
                # (2026-09-23).
                "host_cores": os.cpu_count(),
                # Logical CPUs and physical memory from the Windows API: the
                # most any limit here may be (agent/hardware.py, GitHub #5).
                # A key of its own, not memory_capacity, so placement on
                # Windows is unchanged by it.
                "hardware": hardware.facts("win32"),
                "notes": "one service per runner under its own virtual "
                         "account, in a Job Object, over a directory tree "
                         "only it can read"}


class WindowsRegistrar:
    """Asks the runner service to register under its own account and Job.

    The SYSTEM agent never executes runner-writable scripts or binaries.
    Its fixed client helper only exchanges authenticated JSON bytes.
    """

    def __init__(self, run=None, fs=None, tools=None, storage_backend=None):
        self._run = run or _exec
        self._fs = fs or LocalFs()
        self._tools = dict(TOOLS, **(tools or {}))
        self._storage = storage_backend

    def _script(self, runner_id, name):
        if self._storage:
            self._storage.verify(runner_id)
        reg = naming.names(naming.check(runner_id), "windows")["reg"]
        return ntpath.join(reg, name)

    def register(self, runner_id, plan):
        script = self._script(runner_id, "register.ps1")
        if not self._fs.exists(script):
            raise RuntimeError("the unit has no registration entry point")
        ok, out, err = self._run([self._tools["python"], "-m", "agent.windows_registration",
                                  "--runner-id", naming.check(runner_id)],
                                 input=json.dumps(plan), timeout=registration_limits()["client"])
        if not ok:
            raise RuntimeError(err or "registration failed")
        try:
            answer = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError("the unit did not say how it was registered")
        return {"registration_id": str(answer.get("registration_id") or ""),
                "registration_uuid": answer.get("registration_uuid")}

    def deregister(self, runner_id):
        script = self._script(runner_id, "deregister.ps1")
        if not self._fs.exists(script):
            raise RuntimeError("the unit is gone; its registration can only "
                               "be removed at the forge")
        # Neither supported Windows runner holds a forge-removal credential.
        # The controller deletes the known record directly; no stopped
        # service needs to be restarted merely to repeat this same answer.
        raise RuntimeError("Windows runners cannot remove their forge record; "
                           "the controller must delete it by its registration id")


def _serves_github(spec):
    """Whether the controller made this unit for GitHub, by its label."""
    return ((spec or {}).get("labels") or {}).get("nomercy.provider") == "github"


def _readable(text):
    """NSSM's UTF-16 output as plain text."""
    return (text or "").replace("\x00", "")


def _absent(text):
    """NSSM's and the service manager's ways of saying there is no such
    service."""
    text = text.lower()
    return ("can't open service" in text or "does not exist" in text
            or "1060" in text)
