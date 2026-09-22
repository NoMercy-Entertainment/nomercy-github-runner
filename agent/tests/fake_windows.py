"""A stand-in for a Windows Server worker: its service manager, NSSM, icacls,
PowerShell and its disk, behaving like the real ones where the runtime relies
on them.

The Windows runtime builds argument lists for `nssm`, `sc.exe`, `icacls` and
`powershell.exe`, and reads and writes files. This interprets both against an
in-memory model - services with their account and settings, directories with
their ACLs, files with their sizes - so the runtime and the contract suite can
be exercised without a Windows worker. Strict where Windows is strict:

- installing a service whose name is taken fails with "already exists";
- anything addressed to a service that is not there fails with "Can't open
  service!" (NSSM) or FAILED 1060 (sc.exe);
- stopping a stopped service fails;
- icacls on a path that does not exist fails;
- a file under a directory that does not exist cannot be written.

The template a unit is created from is modelled too: `register.ps1` and
`deregister.ps1` talk to the fake forge, as the real scripts talk to the real
one. The forge and `Crash` are the ones the Linux fake uses.
"""
import json
import ntpath
import time

from .fake_docker import Crash, FakeForge

TOOLS = {"nssm": "nssm.exe", "sc": "sc.exe", "icacls": "icacls.exe",
         "powershell": "powershell.exe", "python": "python.exe",
         "templates": r"C:\ProgramData\nomercy\templates"}
#: One NUL character: what UTF-16 output looks like read as text.
NUL = chr(0)


TEMPLATE = "github-runner-2.328.0"


def _utf16ish(text):
    """What a UTF-16 answer looks like to something reading plain text."""
    if not isinstance(text, str):
        return text
    return NUL.join(text) + (NUL if text else "")


def _key(path):
    return ntpath.normcase(ntpath.normpath(path))


def _under(path, base):
    path, base = _key(path), _key(base)
    return path == base or path.startswith(base + "\\")


class FakeWindows:
    def __init__(self, forge=None):
        self.services = {}
        self.dirs = set()
        self.files = {}         # key -> size
        self.texts = {}         # key -> text, for files written as text
        self.names = {}         # key -> path as given, for listings
        self.acls = {}
        self.locked = set()     # paths whose deletion fails
        self.calls = []
        self.inputs = []
        self.forge = forge or FakeForge()
        self.forge.unit_running = lambda unit: (
            self.services.get(unit, {}).get("state") == "running")
        #: A tool verb (e.g. "install") after which to raise Crash, once.
        self.crash_after = None
        #: unit -> "running" | "aborted": the job its runner has.
        self.jobs = {}
        self.install_template(TEMPLATE)

    # ---- helpers for tests ---------------------------------------------------

    def install_template(self, name):
        base = ntpath.join(TOOLS["templates"], name)
        self.makedirs(base)
        for f in ("run.cmd", "register.ps1", "deregister.ps1"):
            self.put(ntpath.join(base, f), 100)
        self.put(ntpath.join(base, "bin", "Runner.Listener.exe"), 5000)

    def put(self, path, size):
        self.makedirs(ntpath.dirname(path))
        self.files[_key(path)] = size
        self.names[_key(path)] = path

    def size_under(self, base):
        return sum(s for k, s in self.files.items() if _under(k, base))

    def snapshot(self, base):
        """Every file under `base` with its size, and the ACL on `base`."""
        return ({k: s for k, s in self.files.items() if _under(k, base)},
                list(self.acls.get(_key(base), [])))

    # ---- the disk --------------------------------------------------------------

    def exists(self, path):
        k = _key(path)
        return k in self.dirs or k in self.files

    #: Set by a test: a directory this host will not let anyone read.
    explode_on_listdir = False

    def listdir(self, path):
        if self.explode_on_listdir:
            raise OSError("access is denied")
        base = _key(path)
        names = set()
        for k in list(self.dirs) + list(self.files):
            if k != base and k.startswith(base + "\\"):
                names.add(self.names.get(k, k)[len(path.rstrip("\\")) + 1:]
                          .split("\\")[0])
        return sorted(names)

    def makedirs(self, path):
        k = _key(path)
        while k and k not in self.dirs:
            self.dirs.add(k)
            parent = ntpath.dirname(k)
            if parent == k:
                break
            k = parent

    def copytree(self, src, dst):
        if not self.exists(src):
            raise FileNotFoundError(src)
        s, d = _key(src), _key(dst)
        self.makedirs(dst)
        for k in [k for k in self.dirs if _under(k, s)]:
            self.dirs.add(d + k[len(s):])
        for k, size in list(self.files.items()):
            if _under(k, s):
                self.files[d + k[len(s):]] = size

    def write_text(self, path, text):
        if _key(ntpath.dirname(path)) not in self.dirs:
            raise FileNotFoundError(ntpath.dirname(path))
        self.files[_key(path)] = len(text)
        self.texts[_key(path)] = text
        if ntpath.basename(path) == "drain.request":
            # What the job host does when it sees this file: Ctrl+Break to
            # the runner, which finishes a job it has and exits.
            self._drain_requested(self._unit_of(path))

    def remove(self, path):
        self.files.pop(_key(path), None)
        self.texts.pop(_key(path), None)

    @staticmethod
    def _unit_of(path):
        """rnr-<id> for a path inside the runner's own tree under D:."""
        parts = ntpath.normpath(path).split("\\")
        return "rnr-" + parts[2] if len(parts) > 2 else None

    def read_text(self, path):
        k = _key(path)
        if k not in self.files:
            raise FileNotFoundError(path)
        return self.texts.get(k, "")

    def tail(self, path, max_bytes):
        return self.read_text(path)[-max_bytes:]

    def mtime(self, path):
        if _key(path) not in self.files:
            raise FileNotFoundError(path)
        return time.time()

    def _delete_under(self, base, keep_base):
        b = _key(base)
        stuck = [p for p in self.locked if _under(p, base)]
        if stuck:
            raise PermissionError(f"in use: {stuck[0]}")
        for k in [k for k in self.files if _under(k, b)]:
            del self.files[k]
            self.texts.pop(k, None)
        for k in [k for k in self.dirs if _under(k, b)]:
            if not (keep_base and k == b):
                self.dirs.discard(k)
        if not keep_base:
            self.acls.pop(b, None)

    def rmtree(self, path):
        if self.exists(path):
            self._delete_under(path, keep_base=False)

    def clear_dir(self, path):
        if _key(path) in self.dirs:
            self._delete_under(path, keep_base=True)

    def du(self, path):
        if _key(path) not in self.dirs:
            return None
        return self.size_under(path)

    # ---- the programs ----------------------------------------------------------

    def __call__(self, args, input=None, timeout=None):
        args = list(args)
        self.calls.append(args)
        if input is not None:
            self.inputs.append(input)
        tool, rest = args[0], args[1:]
        handler = {TOOLS["nssm"]: self._nssm, TOOLS["sc"]: self._sc,
                   TOOLS["icacls"]: self._icacls,
                   TOOLS["powershell"]: self._powershell,
                   TOOLS["python"]: self._registration_client}.get(tool)
        if handler is None:
            return False, "", f"'{tool}' is not recognized"
        result = handler(rest, input)
        if tool == TOOLS["nssm"]:
            # NSSM writes UTF-16, so whatever reads it as text sees a NUL
            # between every character. The runtime reading that as plain
            # text is how a removal of a service that was never made failed
            # on the first live Windows worker (2026-09-19).
            ok, out, err = result
            result = ok, _utf16ish(out), _utf16ish(err)
        if rest and self.crash_after == rest[0]:
            self.crash_after = None
            raise Crash(f"the agent died after `{tool} {rest[0]}`")
        return result

    def _missing(self, name, sc=False):
        if name in self.services:
            return None
        if sc:
            return False, (f"[SC] OpenService FAILED 1060:\n\nThe specified "
                           f"service does not exist as an installed "
                           f"service."), ""
        return False, "", "Can't open service!"

    def _nssm(self, args, input):
        verb, name = args[0], args[1]
        if verb == "install":
            if name in self.services:
                return False, "", ("Error creating service!\nThe specified "
                                   "service already exists.")
            self.services[name] = {"state": "stopped", "program": args[2],
                                   "args": args[3:], "settings": {},
                                   "account": "LocalSystem", "sidtype": None,
                                   "start": "demand"}
            return True, f"Service \"{name}\" installed successfully!", ""
        missing = self._missing(name)
        if missing:
            return missing
        svc = self.services[name]
        svc.setdefault("draining", False)
        if verb == "set":
            svc["settings"][args[2]] = args[3:]
            return True, f"Set parameter \"{args[2]}\" for service", ""
        if verb == "status":
            return True, {"running": "SERVICE_RUNNING",
                          "stopped": "SERVICE_STOPPED"}[svc["state"]], ""
        if verb == "start":
            if svc["state"] == "running":
                return False, "", (f"{name}: START: An instance of the "
                                   f"service is already running.")
            svc["state"] = "running"
            svc["draining"] = False
            if any(ntpath.basename(p) == "drain.request"
                   and self._unit_of(p) == name for p in self.files):
                # The job host finds the request still there and passes
                # the Ctrl+Break on at once: the runner exits again.
                self._program_exited(name)
            return True, f"{name}: START: The operation completed", ""
        if verb == "stop":
            if svc["state"] != "running":
                return False, "", (f"{name}: STOP: The service has not been "
                                   f"started.")
            self._abort_job(name)
            svc["state"] = "stopped"
            return True, f"{name}: STOP: The operation completed", ""
        if verb == "restart":
            self._abort_job(name)
            svc["state"] = "running"
            svc["draining"] = False
            svc["restarts"] = svc.get("restarts", 0) + 1
            return True, f"{name}: RESTART: done", ""
        if verb == "remove":
            if args[2:] != ["confirm"]:
                return False, "", "remove needs confirm"
            self._abort_job(name)
            del self.services[name]
            return True, f"Service \"{name}\" removed successfully!", ""
        return False, "", f"unknown nssm verb {verb!r}"

    # ---- jobs, and what a drained runner does ---------------------------------

    def _drain_requested(self, unit):
        svc = self.services.get(unit)
        if not svc or svc["state"] != "running":
            return
        if self.jobs.get(unit) == "running":
            svc["draining"] = True
        else:
            self._program_exited(unit)

    def _program_exited(self, unit):
        """The runner exited on its own. NSSM restarts it unless told the
        exit means stop."""
        svc = self.services[unit]
        svc["draining"] = False
        exit_rule = svc["settings"].get("AppExit")
        svc["state"] = ("stopped" if exit_rule == ["Default", "Exit"]
                        else "running")

    def start_job(self, unit):
        if not self.offers(unit):
            return False
        self.jobs[unit] = "running"
        return True

    def finish_job(self, unit):
        state = self.jobs.pop(unit, None)
        if state != "running":
            return False
        if self.services.get(unit, {}).get("draining"):
            self._program_exited(unit)
        return True

    def offers(self, unit):
        svc = self.services.get(unit)
        return (bool(svc) and svc["state"] == "running"
                and not svc.get("draining")
                and self.jobs.get(unit) != "running"
                and bool(self.forge.for_unit(unit)))

    def _abort_job(self, unit):
        if self.jobs.get(unit) == "running":
            self.jobs[unit] = "aborted"

    def _sc(self, args, input):
        verb = args[0]
        if verb == "query":
            lines = []
            for name, svc in sorted(self.services.items()):
                state = {"running": "4  RUNNING",
                         "stopped": "1  STOPPED"}[svc["state"]]
                lines += [f"SERVICE_NAME: {name}", f"DISPLAY_NAME: {name}",
                          "        TYPE               : 10  WIN32_OWN_PROCESS",
                          f"        STATE              : {state}", ""]
            return True, "\n".join(lines), ""
        name = args[1]
        missing = self._missing(name, sc=True)
        if missing:
            return missing
        svc = self.services[name]
        if verb == "config":
            opts = dict(zip(args[2::2], args[3::2]))
            if "obj=" in opts:
                svc["account"] = opts["obj="]
            if "start=" in opts:
                svc["start"] = opts["start="]
            return True, "[SC] ChangeServiceConfig SUCCESS", ""
        if verb == "sidtype":
            svc["sidtype"] = args[2]
            return True, "[SC] ChangeServiceConfig2 SUCCESS", ""
        return False, "", f"unknown sc verb {verb!r}"

    def _icacls(self, args, input):
        path = args[0]
        if not self.exists(path):
            return False, "", f"{path}: The system cannot find the file " \
                              f"specified."
        grants, inherit, i = [], None, 1
        while i < len(args):
            a = args[i]
            if a.startswith("/inheritance:"):
                inherit = a.split(":", 1)[1]
            elif a in ("/grant", "/grant:r"):
                i += 1
                while i < len(args) and not args[i].startswith("/"):
                    grants.append(args[i])
                    i += 1
                continue
            i += 1
        self.acls[_key(path)] = {"inheritance": inherit, "grants": grants}
        return True, "", ""

    def _powershell(self, args, input):
        if any(str(arg).endswith("registration_keys.ps1") for arg in args):
            return True, "{}", ""
        script = args[args.index("-File") + 1]
        if _key(script) not in self.files:
            return False, "", (f"The argument '{script}' to the -File "
                               f"parameter does not exist.")
        reg = ntpath.dirname(script)
        unit = "rnr-" + ntpath.basename(ntpath.dirname(reg))
        marker = ntpath.join(reg, ".runner")
        if ntpath.basename(script) == "register.ps1":
            plan = json.loads(input or "{}")
            try:
                record = self.forge.register(unit, plan)
            except ConnectionError as e:
                return False, "", f"register: {e}"
            self.write_text(marker, record["registration_id"])
            return True, json.dumps({
                "registration_id": record["registration_id"],
                "registration_uuid": record["registration_uuid"]}), ""
        if ntpath.basename(script) == "deregister.ps1":
            if _key(marker) in self.files:
                try:
                    self.forge.delete(self.read_text(marker))
                except ConnectionError as e:
                    return False, "", f"deregister: {e}"
                del self.files[_key(marker)]
            return True, "", ""
        return False, "", f"{script}: not a template entry point"

    def _registration_client(self, args, input):
        if args[:2] != ["-m", "agent.windows_registration"]:
            return False, "", "unknown Python helper"
        rid = args[args.index("--runner-id") + 1]
        name = "rnr-" + rid
        if self.services.get(name, {}).get("state") != "running":
            return False, "", "registration service unavailable"
        # The service's own account executes this fake script, not the agent.
        return self._powershell(["-File", rf"D:\runners\{rid}\reg\register.ps1"], input)
