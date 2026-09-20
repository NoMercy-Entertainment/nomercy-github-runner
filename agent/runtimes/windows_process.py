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
import struct
import subprocess
import sys
import time

from .. import naming
from .localfs import LocalFs

#: Areas created per runner. `docker` is None on Windows: no nested engine.
AREAS = tuple(a for a in naming.AREAS if a != "docker")

#: What a `recreate` keeps. The same rule as Linux (design 15.1): the cache
#: and the logs stay; the workspace, the registration and temp go.
KEPT_ON_RECREATE = ("cache", "logs")

#: Told to the runner, so it knows the layout without being built for it.
LAYOUT_ENV_KEYS = {"work": "RUNNER_WORK_DIR", "cache": "RUNNER_CACHE_DIR",
                   "reg": "RUNNER_REG_DIR", "logs": "RUNNER_LOG_DIR"}

TEMPLATE_FILES = ("run.cmd", "register.ps1", "deregister.ps1")

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

    def __init__(self, run=None, fs=None, tools=None):
        self._run = run or _exec
        self._fs = fs or LocalFs()
        self._tools = dict(TOOLS, **(tools or {}))

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

    def create(self, runner_id, spec):
        rid = naming.check(runner_id)
        name = naming.unit_name(rid)
        image = (spec or {}).get("image")
        if not image:
            raise ValueError("a unit needs a template")
        template = ntpath.join(self._tools["templates"], image)
        if not self._fs.exists(template):
            raise RuntimeError(f"no runner template {image!r} on this worker")
        p = self.paths(rid)

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
        if self.status(rid)["exists"] is not True:
            ok, out, err = self._nssm(
                "install", name, self._tools["python"], "-m", "agent.jobhost",
                "--root", p["root"])
            if not ok and "exists" not in (out + err):
                raise RuntimeError(err or out or "nssm install failed")

        # 3. Storage, locked to this runner before anything is in it.
        self._acl(p["root"], name)

        # 4. The runner's software, once. A re-driven create must not copy
        #    over a registered runner's files while its service holds them.
        marker = ntpath.join(p["reg"], TEMPLATE_MARKER)
        if not self._fs.exists(marker):
            self._fs.copytree(template, p["reg"])
            self._fs.write_text(marker, image)

        # 5. What the job host reads: limits and environment, never argv.
        self._fs.write_text(ntpath.join(p["reg"], "unit.json"),
                            json.dumps(self._unit(rid, spec, p)))

        self._configure(name, p)

        if not self.status(rid).get("running"):
            self._check(self._nssm("start", name, timeout=STOP_TIMEOUT + 30))
        return name

    def _unit(self, rid, spec, p):
        env = dict(spec.get("env") or {})
        for area, key in LAYOUT_ENV_KEYS.items():
            env[key] = p[area]
        env["TEMP"] = env["TMP"] = p["tmp"]
        cpus = str(spec.get("cpus") or "").strip()
        return {"runner_id": rid, "env": env,
                "memory_bytes": size_bytes(spec.get("memory")),
                "cpus": float(cpus) if cpus not in ("", "0") else None,
                "cpuset": spec.get("cpuset") or None}

    def _acl(self, root, name):
        """Only LocalSystem, Administrators and this runner's own account, and
        nothing inherited from above: another runner's account is not in the
        list, so it cannot read this tree."""
        self._check(self._run([
            self._tools["icacls"], root, "/inheritance:r", "/grant:r",
            f"*{SYSTEM_SID}:(OI)(CI)F", f"*{ADMINISTRATORS_SID}:(OI)(CI)F",
            f"*{service_sid(name)}:(OI)(CI)M", "/Q"], timeout=120))

    def _configure(self, name, p):
        self._check(self._sc("config", name, "obj=", f"NT SERVICE\\{name}",
                             "start=", "auto"))
        self._check(self._sc("sidtype", name, "unrestricted"))
        log = ntpath.join(p["logs"], "runner.log")
        for setting in (("AppDirectory", p["work"]),
                        ("AppStdout", log), ("AppStderr", log),
                        # Ctrl+C first, and the grace a deregistration needs.
                        ("AppStopMethodConsole", str(STOP_TIMEOUT * 1000)),
                        ("AppExit", "Default", "Restart")):
            self._check(self._nssm("set", name, *setting))

    def start(self, runner_id):
        """Started, and in service. What a drain leaves behind is undone
        first - the request file the job host would answer with Ctrl+Break,
        and NSSM's leave-it-down on exit - so a service started after a drain
        serves instead of exiting again at once. A running service is left
        running: NSSM refuses to start one twice."""
        name = naming.unit_name(runner_id)
        p = self.paths(runner_id)
        self._fs.remove(ntpath.join(p["reg"], DRAIN_REQUEST))
        self._check(self._nssm("set", name, "AppExit", "Default", "Restart"))
        if not self.status(runner_id).get("running"):
            self._check(self._nssm("start", name, timeout=STOP_TIMEOUT + 30))

    def stop(self, runner_id):
        """Ctrl+C to the runner, then the grace a deregistration needs. A
        service that is already down - a drained runner's, say - is left as
        it is: NSSM refuses to stop one that has not been started, and a stop
        is asked for to make the unit down, which it is."""
        if not self.status(runner_id).get("running"):
            return
        self._check(self._nssm("stop", naming.unit_name(runner_id),
                               timeout=STOP_TIMEOUT + 30))

    def restart(self, runner_id):
        self._check(self._nssm("restart", naming.unit_name(runner_id),
                               timeout=2 * STOP_TIMEOUT + 30))

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
        if self.status(rid)["exists"] is not False:
            # Stopped first, so the runner gets its grace; a service that is
            # already stopped makes this fail, which changes nothing.
            self._nssm("stop", name, timeout=STOP_TIMEOUT + 30)
            ok, out, err = self._nssm("remove", name, "confirm")
            if not ok and not _absent(out + err):
                raise RuntimeError(err or out or "nssm remove failed")
        p = self.paths(rid)
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
        return {"exists": True, "running": state == "SERVICE_RUNNING",
                "state": {"SERVICE_RUNNING": "running",
                          "SERVICE_STOPPED": "exited",
                          "SERVICE_PAUSED": "paused",
                          "SERVICE_START_PENDING": "starting",
                          "SERVICE_STOP_PENDING": "stopping"}.get(state,
                                                                  "unknown")}

    def telemetry(self, runner_id):
        """What the job host last measured, if it is recent. A stale or
        missing report is unknown, not zero."""
        result = {"cpu_percent": None, "mem_used_bytes": None,
                  "mem_limit_bytes": None}
        p = self.paths(runner_id)
        try:
            unit = json.loads(self._fs.read_text(
                ntpath.join(p["reg"], "unit.json")))
            result["mem_limit_bytes"] = unit.get("memory_bytes")
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

    def logs(self, runner_id, since_seconds, max_bytes=256 * 1024):
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

    def probe(self, runner_id, probe):
        p = self.paths(runner_id)
        if probe == "disk_usage":
            value = self._fs.du(p["root"])
        elif probe == "cache_size":
            value = self._fs.du(p["cache"])
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
        return {"ok": True, "value": value}

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
                                        "STOPPED": "stopped",
                                        "PAUSED": "stopped"}.get(state,
                                                                 "unknown")})
                current = None
        return found

    # ---- cache ---------------------------------------------------------------

    def clear_cache(self, runner_id, policy):
        """Clear the named scopes, each one a directory this runner owns.

        A scope this runtime cannot clear is reported as an error for that
        scope, never skipped in silence. A failing scope does not stop the
        others. A second call finds nothing to free and still succeeds.
        """
        where = self.scope_locations(runner_id)
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
                # OPEN-7: a graceful stop that stays stopped.
                "supports_drain": True,
                "clear_cache": True,
                "cache_scopes": sorted(SUPPORTED_SCOPES),
                "notes": "one service per runner under its own virtual "
                         "account, in a Job Object, over a directory tree "
                         "only it can read"}


class WindowsRegistrar:
    """Registers the runner through its template's `register.ps1`, with the
    plan - token included - on standard input, never in an argument list."""

    def __init__(self, run=None, fs=None, tools=None):
        self._run = run or _exec
        self._fs = fs or LocalFs()
        self._tools = dict(TOOLS, **(tools or {}))

    def _script(self, runner_id, name):
        reg = naming.names(naming.check(runner_id), "windows")["reg"]
        return ntpath.join(reg, name)

    def _powershell(self, script, input=None, timeout=120):
        return self._run([self._tools["powershell"], "-NoProfile",
                          "-NonInteractive", "-ExecutionPolicy", "Bypass",
                          "-File", script], input=input, timeout=timeout)

    def register(self, runner_id, plan):
        script = self._script(runner_id, "register.ps1")
        if not self._fs.exists(script):
            raise RuntimeError("the unit has no registration entry point")
        ok, out, err = self._powershell(script, input=json.dumps(plan))
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
        ok, out, err = self._powershell(script, timeout=60)
        if not ok:
            raise RuntimeError(err or out or "deregistration failed")


def _readable(text):
    """NSSM's UTF-16 output as plain text."""
    return (text or "").replace("\x00", "")


def _absent(text):
    """NSSM's and the service manager's ways of saying there is no such
    service."""
    text = text.lower()
    return ("can't open service" in text or "does not exist" in text
            or "1060" in text)
