"""A stand-in for the macOS appliance: the guest's launchd, ps, df and disk,
and the hypervisor side that boots it.

The macOS runtime builds argument lists for `launchctl`, `ps` and `df`, runs
its template's `register` and `deregister`, and reads and writes files. This
interprets all of that against an in-memory guest, strict where launchd is:

- `print` of a job launchd does not have fails with "Could not find service"
  and exit 113;
- `bootstrap` of a job already loaded fails with "5: Input/output error" -
  launchd's famously unhelpful way of saying so - and of a plist that is not
  there with "2: No such file or directory";
- `kickstart` of a job that is not loaded fails;
- a file under a directory that does not exist cannot be written.

`FakeAppliance` is the hypervisor side: a guest that is up or down, and the
boot leftover a hard stop leaves, which makes the next boot loop - as the
real one did (design 9.3.1).
"""
import fnmatch
import json
import plistlib
import posixpath
import time

from .fake_docker import Crash, FakeForge

TOOLS = {"launchctl": "/bin/launchctl", "ps": "/bin/ps", "df": "/bin/df",
         "templates": "/Users/runner/templates",
         "launch_agents": "/Users/runner/Library/LaunchAgents",
         "domain": "gui/501"}
TEMPLATE = "forgejo-runner-darwin-amd64-v12.0.1"


def _under(path, base):
    path, base = posixpath.normpath(path), posixpath.normpath(base)
    return path == base or path.startswith(base.rstrip("/") + "/")


class FakeAppliance:
    def __init__(self):
        self.power = "running"
        self.leftovers = []
        self.boots = 0

    def state(self):
        return self.power

    def hard_stop(self):
        self.power = "stopped"
        self.leftovers.append("/var/tmp/opencore-image-ng.sh-49")

    def clear_boot_leftovers(self):
        gone = [p for p in self.leftovers
                if fnmatch.fnmatch(p, "/var/tmp/opencore-image-ng.sh-*")]
        self.leftovers = [p for p in self.leftovers if p not in gone]
        return gone

    def boot(self):
        if self.leftovers:
            raise RuntimeError("the guest loops at boot: a leftover from a "
                               "hard stop is still there")
        self.power = "running"
        self.boots += 1


class FakeMac:
    def __init__(self, forge=None, appliance=None):
        self.dirs = {"/"}
        self.files = {}         # path -> size
        self.texts = {}         # path -> text
        self.modes = {}
        self.locked = set()
        self.jobs = {}          # label -> {"state", "pid", "plist"}
        self.calls = []
        self.inputs = []
        self.root_disk = (100 * 1024 ** 3, 60 * 1024 ** 3)
        self.appliance = appliance or FakeAppliance()
        self.forge = forge or FakeForge()
        self.forge.unit_running = self._unit_running
        self._next_pid = 500
        #: A launchctl verb after which to raise Crash, once.
        self.crash_after = None
        #: unit -> "running" | "aborted": the runner's own job, not launchd's.
        self.work = {}
        self.install_template(TEMPLATE)

    def _unit_running(self, unit):
        job = self.jobs.get("com.nomercy." + unit)
        return (self.appliance.power == "running" and job is not None
                and job["state"] == "running")

    # ---- helpers for tests ---------------------------------------------------

    def install_template(self, name):
        base = posixpath.join(TOOLS["templates"], name)
        for f in ("run", "register", "deregister"):
            self.put(posixpath.join(base, f), 100)
        self.put(posixpath.join(base, "forgejo-runner"), 5000)

    def put(self, path, size):
        self.makedirs(posixpath.dirname(path))
        self.files[path] = size

    def size_under(self, base):
        return sum(s for p, s in self.files.items() if _under(p, base))

    def snapshot(self, base):
        return {p: s for p, s in self.files.items() if _under(p, base)}

    # ---- the disk --------------------------------------------------------------

    def exists(self, path):
        return path in self.dirs or path in self.files

    def makedirs(self, path):
        while path and path not in self.dirs:
            self.dirs.add(path)
            path = posixpath.dirname(path)

    def chmod(self, path, mode):
        if not self.exists(path):
            raise FileNotFoundError(path)
        self.modes[path] = mode

    def listdir(self, path):
        names = {p[len(path.rstrip("/")) + 1:].split("/")[0]
                 for p in list(self.files) + list(self.dirs)
                 if _under(p, path) and p != path}
        return sorted(names)

    def copytree(self, src, dst):
        if src not in self.dirs:
            raise FileNotFoundError(src)
        self.makedirs(dst)
        for d in [d for d in self.dirs if _under(d, src)]:
            self.dirs.add(dst + d[len(src):])
        for p, s in list(self.files.items()):
            if _under(p, src):
                self.files[dst + p[len(src):]] = s

    def write_text(self, path, text, mode=None):
        if posixpath.dirname(path) not in self.dirs:
            raise FileNotFoundError(posixpath.dirname(path))
        self.files[path] = len(text)
        self.texts[path] = text
        if mode is not None:
            self.modes[path] = mode

    def read_text(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.texts.get(path, "")

    def tail(self, path, max_bytes):
        return self.read_text(path)[-max_bytes:]

    def mtime(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return time.time()

    def remove(self, path):
        self.files.pop(path, None)
        self.texts.pop(path, None)

    def _delete_under(self, base, keep_base):
        stuck = [p for p in self.locked if _under(p, base)]
        if stuck:
            raise PermissionError(f"in use: {stuck[0]}")
        for p in [p for p in self.files if _under(p, base)]:
            self.remove(p)
        for d in [d for d in self.dirs if _under(d, base)]:
            if not (keep_base and d == base):
                self.dirs.discard(d)

    def rmtree(self, path):
        if self.exists(path):
            self._delete_under(path, keep_base=False)

    def clear_dir(self, path):
        if path in self.dirs:
            self._delete_under(path, keep_base=True)

    def du(self, path):
        if path not in self.dirs:
            return None
        return self.size_under(path)

    # ---- the programs ----------------------------------------------------------

    def __call__(self, args, input=None, timeout=None):
        args = list(args)
        self.calls.append(args)
        if input is not None:
            self.inputs.append(input)
        if self.appliance.power != "running":
            return False, "", "ssh: connect to host: Connection refused"
        tool = args[0]
        if tool == TOOLS["launchctl"]:
            result = self._launchctl(args[1:])
            if self.crash_after == args[1]:
                self.crash_after = None
                raise Crash(f"the agent died after `launchctl {args[1]}`")
            return result
        if tool == TOOLS["ps"]:
            return True, "\n".join(
                f"{j['pid']:>5} 2.5 102400" for j in self.jobs.values()
                if j["state"] == "running"), ""
        if tool == TOOLS["df"]:
            total, used = self.root_disk
            return True, ("Filesystem 1024-blocks Used Available Capacity "
                          "Mounted on\n"
                          f"/dev/disk3s1s1 {total // 1024} {used // 1024} "
                          f"{(total - used) // 1024} 60% /"), ""
        return self._entry(args, input)

    def _launchctl(self, args):
        verb = args[0]
        if verb == "bootstrap":
            plist_path = args[2]
            if plist_path not in self.files:
                return False, "", "Bootstrap failed: 2: No such file or " \
                                  "directory"
            job = plistlib.loads(self.read_text(plist_path).encode())
            label = job["Label"]
            if label in self.jobs:
                return False, "", "Bootstrap failed: 5: Input/output error"
            self.jobs[label] = {"state": "waiting", "pid": None, "job": job}
            if job.get("RunAtLoad"):
                self._run_job(label)
            return True, "", ""
        target = (args[-1] if verb == "kill" else
                  [a for a in args[1:] if not a.startswith("-")][0])
        label = target.split("/", 2)[-1]
        if label not in self.jobs:
            return False, "", (f'Could not find service "{label}" in domain '
                               f'for port')
        job = self.jobs[label]
        if verb == "print":
            lines = [f"{target} = {{", f"\tstate = {job['state']}"]
            if job["pid"]:
                lines.append(f"\tpid = {job['pid']}")
            return True, "\n".join(lines + ["}"]), ""
        if verb == "kickstart":
            if "-k" in args:
                self._abort_work(label)
            elif job["state"] == "running":
                return True, "", ""     # launchd: already running, left be
            self._run_job(label)
            return True, "", ""
        if verb == "bootout":
            self._abort_work(label)
            del self.jobs[label]
            return True, "", ""
        if verb == "kill":
            # SIGTERM to the runner: it takes nothing new, finishes a job it
            # has, and exits cleanly - which KeepAlive does not restart.
            if job["state"] != "running":
                return True, "", ""
            if self.work.get(self._unit(label)) == "running":
                job["draining"] = True
            else:
                self._exited_cleanly(label)
            return True, "", ""
        return False, "", f"unknown launchctl verb {verb!r}"

    def _run_job(self, label):
        self._next_pid += 1
        self.jobs[label].update(state="running", pid=self._next_pid,
                                draining=False)

    def _exited_cleanly(self, label):
        self.jobs[label].update(state="waiting", pid=None, draining=False)

    def crashed(self, unit):
        """The runner exits uncleanly. KeepAlive's `SuccessfulExit: false`
        is launchd's cue to start it again."""
        label = "com.nomercy." + unit
        job = self.jobs.get(label)
        if not job or job["state"] != "running":
            return
        self._abort_work(label)
        keep_alive = job["job"].get("KeepAlive")
        if isinstance(keep_alive, dict) and \
                keep_alive.get("SuccessfulExit") is False:
            self._run_job(label)
        else:
            self._exited_cleanly(label)

    @staticmethod
    def _unit(label):
        return label[len("com.nomercy."):]

    # ---- the runner's jobs ----------------------------------------------------

    def start_job(self, unit):
        if not self.offers(unit):
            return False
        self.work[unit] = "running"
        return True

    def finish_job(self, unit):
        state = self.work.pop(unit, None)
        if state != "running":
            return False
        label = "com.nomercy." + unit
        if self.jobs.get(label, {}).get("draining"):
            self._exited_cleanly(label)
        return True

    def offers(self, unit):
        job = self.jobs.get("com.nomercy." + unit)
        return (self.appliance.power == "running" and job is not None
                and job["state"] == "running" and not job.get("draining")
                and self.work.get(unit) != "running"
                and bool(self.forge.for_unit(unit)))

    def _abort_work(self, label):
        unit = self._unit(label)
        if self.work.get(unit) == "running":
            self.work[unit] = "aborted"

    def _entry(self, args, input):
        entry = args[0]
        if entry not in self.files:
            return False, "", f"{entry}: No such file or directory"
        reg = posixpath.dirname(entry)
        unit = "rnr-" + posixpath.basename(posixpath.dirname(reg))
        marker = posixpath.join(reg, ".runner")
        name = posixpath.basename(entry)
        if name == "register":
            plan = json.loads(input or "{}")
            try:
                record = self.forge.register(unit, plan)
            except ConnectionError as e:
                return False, "", f"register: {e}"
            self.write_text(marker, record["registration_id"])
            return True, json.dumps({
                "registration_id": record["registration_id"],
                "registration_uuid": record["registration_uuid"]}), ""
        if name == "deregister":
            if marker in self.files:
                try:
                    self.forge.delete(self.read_text(marker))
                except ConnectionError as e:
                    return False, "", f"deregister: {e}"
                self.remove(marker)
            return True, "", ""
        return False, "", f"{entry}: not a template entry point"
