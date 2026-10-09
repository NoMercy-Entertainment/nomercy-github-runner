"""macOS runners inside the macOS appliance: a guest operating system, not a
unit on an engine.

The appliance is the `macos-sequoia` guest (design 9.3, 10.6). The agent runs
inside it, as the runner's own user, and each runner instance is a **launchd
job** in that user's domain, over **its own directory tree** under
`/Users/runner/runners/<runner_id>/` - `work`, `cache`, `reg`, `logs`, and
`tmp` for its TMPDIR. There is no nested engine; macOS runners cannot run job
images (design 9.5), and `capabilities()` says so rather than the dashboard
hiding it.

**The five points of design 10.6**, which are the whole uniformity claim:

1. one control agent speaking section 13's protocol - this runtime behind the
   same `Agent` and `AgentServer` as the other two;
2. `status`, `telemetry`, `logs` and the closed probe set - below;
3. `create`, `start`, `stop`, `remove`, `clear_cache` with the same semantics
   and the same idempotency - the contract suite holds it to that;
4. honest `capabilities()`, including what it cannot do;
5. a `runtime_template` naming what an instance was made from - the template
   recorded at create and reported by `status`.

**The appliance's own power** - booting and shutting down the guest - is not
something a runner inside it can do, so it arrives as an `ApplianceHost`, the
hypervisor side. When one is attached, `create` and `start` make sure the
guest is up first, and clear the boot leftover the recorded restart loop
comes from (`/var/tmp/opencore-image-ng.sh-*` after a hard stop, design
9.3.1) before booting. When none is attached, the agent answering at all is
the proof the guest is up. The concrete host side is T-0803's.

**Telemetry includes the volume the runners live on**, because a full one is
the other recorded failure - the runner reads offline while the hypervisor
side looks healthy - and the guest is the only place that sees it. It is the
Data volume holding the runner's tree, never `/`: on APFS that is the sealed
system volume, whose figures say nothing about the disk that fills.

**The runner's software is a template**, a directory named by the spec's
`image` under `/Users/runner/templates/`, copied into `reg` at create: `run`,
which starts the runner once it is registered; `register`, which reads the
registration plan as JSON on standard input and answers
`{"registration_id", "registration_uuid"}`; and `deregister`. The same two
fixed entry points as the other runtimes, so this module never learns which
forge a runner serves. The self-built `forgejo-runner-darwin-*` artefact
becomes one such template (T-0805).

Nothing here has run inside the real appliance yet; that is MACOS-ENV
(T-0802, T-0803). It is exercised against a fake guest.
"""
import json
import os
import plistlib
import posixpath
import subprocess
import time
from pathlib import Path
from typing import Protocol

from .. import naming
from ..jobs import current_job
from .localfs import LocalFs

#: The areas an appliance instance has: every one in naming except the
#: nested engine's, which it does not have. Asserted against naming by test.
AREAS = ("work", "cache", "reg", "logs")
KEPT_ON_RECREATE = ("cache", "logs")

LAYOUT_ENV_KEYS = {"work": "RUNNER_WORK_DIR", "cache": "RUNNER_CACHE_DIR",
                   "reg": "RUNNER_REG_DIR", "logs": "RUNNER_LOG_DIR"}
TEMPLATE_MARKER = ".template"

#: GitHub's job hooks (#7): each variable and the script it names, in the
#: runner's reg directory under `hooks/`. The guest has none of the agent's
#: files, so every create puts the agent's own copy there, `lib.sh` beside
#: them. GitHub runs a hook only when its path ends in .sh, .ps1 or .js.
#: Forgejo has no hooks.
HOOK_SOURCE = Path(__file__).resolve().parents[1] / "hooks" / "macos"
HOOK_SCRIPTS = {"ACTIONS_RUNNER_HOOK_JOB_STARTED": "job-started.sh",
                "ACTIONS_RUNNER_HOOK_JOB_COMPLETED": "job-completed.sh"}
HOOK_FILES = ("lib.sh", "runner_guard.js", *HOOK_SCRIPTS.values())
#: Where a runner's hooks go when its launchd job is a system one: a tree
#: root owns, which the runner's account - and so a job - cannot change.
#: A runner in a user's own launchd domain keeps them in its reg directory,
#: which that user owns: there is no privileged path to write them with.
SYSTEM_HOOK_ROOT = "/Library/Nomercy/runner-hooks"
#: The file, and the line in it, that say which origin check a runner's
#: hooks carry.
GUARD_FILE = "runner_guard.js"
GUARD_PREFIX = "const GUARD_VERSION = "
#: What an adopted instance is: the launchd job and the directory that were
#: already there when the controller took it over (MIG-4). Written by
#: `create` when its spec carries an `adopt` block, and read by every verb
#: afterwards, so a runner installed by hand is driven like any other
#: without being rebuilt.
ADOPTED_MARKER = ".adopted"
LABEL_PREFIX = "com.nomercy."

#: Which of the runner's own directories each clearable scope is (T-1601);
#: the scopes offered are generated from this table. Only what provably
#: belongs to one runner: Xcode's DerivedData lives in the user's Library,
#: shared by every instance in the guest, so it cannot be attributed and is
#: not offered (design 15.2).
SCOPE_AREAS = {"workspace": "work", "toolcache": "cache", "temp": "tmp"}
SUPPORTED_SCOPES = frozenset(SCOPE_AREAS)
DEFAULT_SCOPES = ("toolcache", "temp")

STOP_TIMEOUT = 60

#: The file a hard stop leaves behind, which makes the next boot loop.
#: MEASURED, docs in memory: macos-runner-opencore-restart-loop.
BOOT_LEFTOVER = "/var/tmp/opencore-image-ng.sh-*"

TOOLS = {
    "launchctl": "/bin/launchctl",
    "ps": "/bin/ps",
    "df": "/bin/df",
    "templates": "/Users/runner/templates",
    "launch_agents": "/Users/runner/Library/LaunchAgents",
    "domain": None,             # gui/<uid> or user/<uid>; see _domain
    "runner_user": None,        # required for system launchd jobs
}


class ApplianceHost(Protocol):
    """The hypervisor side: validates pre-boot cleanup and boots the guest."""

    def state(self) -> str: ...                 # running | stopped | unknown
    def clear_boot_leftovers(self) -> list: ...
    def boot(self) -> None: ...


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


class MacApplianceRuntime:
    kind = "macos-appliance"

    def __init__(self, run=None, fs=None, appliance=None, tools=None, remote=False):
        self._run = run or _exec
        self._fs = fs or LocalFs()
        self._appliance = appliance
        self._remote = remote
        self._tools = dict(TOOLS, **(tools or {}))
        if self._tools["domain"] == "system" and not self._tools["runner_user"]:
            raise ValueError("system launchd jobs require an explicit runner_user")

    # ---- where things are ----------------------------------------------------

    def paths(self, runner_id):
        """Every path this runner owns, derived from its runner_id alone."""
        rid = naming.check(runner_id)
        areas = naming.names(rid, "macos")
        root = posixpath.dirname(areas["work"])
        out = {area: areas[area] for area in AREAS}
        out["root"] = root
        out["tmp"] = posixpath.join(root, "tmp")
        out["plist"] = posixpath.join(self._tools["launch_agents"],
                                      f"{self.label(rid)}.plist")
        return out

    def scope_locations(self, runner_id):
        """Each scope's directory: always inside this runner's own tree."""
        p = self.paths(runner_id)
        return {scope: p[area] for scope, area in SCOPE_AREAS.items()}

    @staticmethod
    def label(runner_id):
        return LABEL_PREFIX + naming.unit_name(runner_id)

    def adopted(self, runner_id):
        """What this instance was adopted from, or None when this runtime
        made it itself. Read from the guest, so an agent restart does not
        forget which job a spec means."""
        rid = naming.check(runner_id)
        areas = naming.names(rid, "macos")
        marker = posixpath.join(posixpath.dirname(areas["work"]),
                                ADOPTED_MARKER)
        try:
            return json.loads(self._fs.read_text(marker))
        except (OSError, ValueError):
            return None

    def _label_of(self, runner_id):
        """The launchd label this instance is: its own, or the one it was
        adopted from."""
        record = self.adopted(runner_id)
        return (record or {}).get("label") or self.label(runner_id)

    def _domain(self):
        if self._tools["domain"]:
            return self._tools["domain"]
        if self._remote:
            raise RuntimeError("remote appliance requires its guest's explicit launchd domain")
        return f"gui/{os.getuid()}"

    def _target(self, runner_id):
        return f"{self._domain()}/{self._label_of(runner_id)}"

    def _launchctl(self, *args, timeout=30):
        return self._run([self._tools["launchctl"], *args], timeout=timeout)

    # ---- the appliance as a whole --------------------------------------------

    def _appliance_up(self):
        """Boot the guest if it is down, clearing the leftover that makes a
        boot after a hard stop loop. Nothing to do when this runs inside it."""
        if self._appliance is None:
            return
        state = self._appliance.state()
        if state == "running":
            if hasattr(self._appliance, "wait_ready"):
                self._appliance.wait_ready()
            return
        if state == "unknown":
            raise RuntimeError("the appliance's power state is unknown; not "
                               "booting it blind")
        self._appliance.clear_boot_leftovers()
        self._appliance.boot()

    # ---- lifecycle -----------------------------------------------------------

    def create(self, runner_id, spec):
        rid = naming.check(runner_id)
        if (spec or {}).get("adopt"):
            return self._adopt(rid, spec["adopt"])
        image = (spec or {}).get("image")
        if not image:
            raise ValueError("an instance needs a template")
        template = posixpath.join(self._tools["templates"], image)
        self._appliance_up()
        if image not in self._templates():
            raise RuntimeError(f"no runner template {image!r} in this "
                               f"appliance")
        p = self.paths(rid)

        # 1. Storage, private to this user before anything is in it.
        self._fs.makedirs(p["root"])
        self._fs.chmod(p["root"], 0o700)
        for key in (*AREAS, "tmp"):
            self._fs.makedirs(p[key])

        # 2. The runner's software, once.
        marker = posixpath.join(p["reg"], TEMPLATE_MARKER)
        if not self._fs.exists(marker):
            self._fs.copytree(template, p["reg"])
            self._fs.write_text(marker, image)

        # 2b. GitHub's job hooks, before the job that points at them, and on
        #     every create, so a redeployed agent's copy reaches the guest.
        if _serves_github(spec):
            self._install_hooks(p, rid)

        # 3. The launchd job. Its environment can carry a token, so the file
        #    is readable by this user alone.
        self._fs.makedirs(self._tools["launch_agents"])
        plist_text = self._plist(rid, spec, p)
        if self._domain() == "system":
            # launchd refuses a system plist owned by the unprivileged user.
            # The staging file is private; install sets ownership atomically.
            stage = posixpath.join(p["reg"], ".launchd.plist")
            self._fs.write_text(stage, plist_text, mode=0o600)
            self._check(self._run(["/usr/bin/sudo", "-n", "/usr/bin/install",
                                  "-o", "root", "-g", "wheel", "-m", "0644",
                                  stage, p["plist"]]))
            self._fs.remove(stage)
        else:
            self._fs.write_text(p["plist"], plist_text, mode=0o600)

        # 4. Loaded and running, from whatever state a cut-off create left.
        self._check(self._launchctl("enable", self._target(rid)))
        self._load(rid)
        if not self.status(rid).get("running"):
            self._check(self._launchctl("kickstart", self._target(rid)))
        return naming.unit_name(rid)

    def _adopt(self, rid, adopt):
        """Take over a runner that is already installed and serving.

        It keeps its launchd job, its directory and its registration; what
        is written here is the record that says so, and the tree this
        runtime keeps its own data in. Nothing is loaded, kickstarted or
        stopped: a runner with a job running must not notice this at all.
        Idempotent - adopting twice writes the same record."""
        label = (adopt or {}).get("label")
        if not label:
            raise ValueError("adopting needs the launchd label of the job "
                             "that is already there")
        self._appliance_up()
        ok, out, err = self._launchctl("print",
                                       f"{self._domain()}/{label}")
        if not ok:
            raise RuntimeError(f"no launchd job {label!r} in this appliance "
                               f"to adopt: {err or out}".strip())
        # Where its definition lives is launchd's answer, not something the
        # controller told this worker: a path is a worker's own business
        # (13.3), and the job knows its own.
        plist = _field(out, "path")
        p = self.paths(rid)
        self._fs.makedirs(p["root"])
        self._fs.chmod(p["root"], 0o700)
        for key in ("logs",):
            self._fs.makedirs(p[key])
        record = {"label": label,
                  "template": (adopt or {}).get("template"),
                  # Kept so an instance that has been stopped can be loaded
                  # again. Without it, stopping an adopted runner would be a
                  # one-way door.
                  "plist": plist,
                  "adopted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime())}
        self._fs.write_text(posixpath.join(p["root"], ADOPTED_MARKER),
                            json.dumps(record, sort_keys=True), mode=0o600)
        return naming.unit_name(rid)

    def _hooks_dir(self, p, rid):
        if self._domain() == "system":
            return posixpath.join(SYSTEM_HOOK_ROOT, rid)
        return posixpath.join(p["reg"], "hooks")

    def _install_hooks(self, p, rid):
        hooks = self._hooks_dir(p, rid)
        if self._domain() != "system":
            self._fs.makedirs(hooks)
            for name in HOOK_FILES:
                text = (HOOK_SOURCE / name).read_text(encoding="utf-8").replace("\r\n", "\n")
                self._fs.write_text(posixpath.join(hooks, name), text,
                                    mode=0o700 if name in HOOK_SCRIPTS.values() else 0o600)
            return
        # Root's, readable by all, writable by root alone: the runner runs a
        # .sh hook through bash and the guard through node, so nothing needs
        # the execute bit. Staged in the runner's own tree, privately, and
        # installed as the system plist is.
        self._check(self._run(["/usr/bin/sudo", "-n", "/usr/bin/install", "-d",
                               "-o", "root", "-g", "wheel", "-m", "0755", hooks]))
        for name in HOOK_FILES:
            text = (HOOK_SOURCE / name).read_text(encoding="utf-8").replace("\r\n", "\n")
            stage = posixpath.join(p["reg"], ".hook-" + name)
            self._fs.write_text(stage, text, mode=0o600)
            try:
                self._check(self._run(["/usr/bin/sudo", "-n", "/usr/bin/install",
                                       "-o", "root", "-g", "wheel", "-m", "0644",
                                       stage, posixpath.join(hooks, name)]))
            finally:
                self._fs.remove(stage)

    def origin_guard_report(self, runner_id):
        """Whether this runner's job-started hook refuses outside code
        (agent/origin_guard.py): every file in the hooks directory its
        launchd job names must still be this agent's own copy, and its log
        directory holds the hook's record of its last run. Read afresh on
        every deep pass, over the guest's SSH when the agent is outside it.
        None for a runner that is not GitHub's (no hook in its launchd job,
        an adopted one included) or that cannot be read now."""
        from ..origin_guard import LAST_RESULT, measure_tree
        rid = naming.check(runner_id)
        p = self.paths(rid)
        try:
            if not self._fs.exists(p["plist"]):
                return None
            job = plistlib.loads(self._fs.read_text(p["plist"]).encode("utf-8"))
            hook = (job.get("EnvironmentVariables") or {}).get(
                "ACTIONS_RUNNER_HOOK_JOB_STARTED")
            if not hook:
                return None
            expected = {name: (HOOK_SOURCE / name).read_text(encoding="utf-8")
                        for name in HOOK_FILES}
            return measure_tree(self._fs, posixpath.dirname(hook), expected,
                                posixpath.join, GUARD_FILE, GUARD_PREFIX,
                                posixpath.join(p["logs"], LAST_RESULT))
        except (OSError, ValueError, RuntimeError, AttributeError, plistlib.InvalidFileException):
            return None

    def _plist(self, rid, spec, p):
        env = dict(spec.get("env") or {})
        for area, key in LAYOUT_ENV_KEYS.items():
            env[key] = p[area]
        env["TMPDIR"] = p["tmp"] + "/"
        env["HOME"] = posixpath.join(p["work"], ".home")
        shared_home = ("/Users/" + self._tools["runner_user"]
                       if self._tools.get("runner_user") else os.path.expanduser("~"))
        for key, directory in {
            "CARGO_HOME": "cargo", "DOTNET_CLI_HOME": "dotnet",
            "NUGET_PACKAGES": "nuget", "NUGET_HTTP_CACHE_PATH": "nuget-http",
            "NUGET_SCRATCH": "nuget-scratch", "GRADLE_USER_HOME": "gradle",
            "NPM_CONFIG_CACHE": "npm", "PIP_CACHE_DIR": "pip",
            "GOCACHE": "go-build", "GOMODCACHE": "go-mod", "GOPATH": "go",
            "XDG_DATA_HOME": "xdg-data", "XDG_CONFIG_HOME": "xdg-config",
        }.items():
            env[key] = posixpath.join(p["cache"], directory)
        for key, directory in {
            "RUSTUP_HOME": ".rustup", "PYENV_ROOT": ".pyenv",
            "RBENV_ROOT": ".rbenv", "ANDROID_HOME": "Library/Android/sdk",
            "ANDROID_SDK_ROOT": "Library/Android/sdk",
        }.items():
            env.setdefault(key, posixpath.join(shared_home, directory))
        env["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1"
        env["DOTNET_GENERATE_ASPNET_CERTIFICATE"] = "false"
        if _serves_github(spec):
            hooks = self._hooks_dir(p, rid)
            for key, name in HOOK_SCRIPTS.items():
                env[key] = posixpath.join(hooks, name)
            # The account's own home, where Xcode keeps DerivedData; the
            # job's HOME is the runner's, under its work directory. From
            # outside the guest only runner_user names it - the agent's own
            # home is the appliance host's - and unknown, it is left unset,
            # so the hooks leave the account's DerivedData alone.
            if self._tools.get("runner_user"):
                env["RUNNER_HOOK_USER_HOME"] = "/Users/" + self._tools["runner_user"]
            elif not self._remote:
                env["RUNNER_HOOK_USER_HOME"] = os.path.expanduser("~")
        log = posixpath.join(p["logs"], "runner.log")
        job = {"Label": self.label(rid),
               "ProgramArguments": [posixpath.join(p["reg"], "run")],
               "WorkingDirectory": p["work"],
               "EnvironmentVariables": env,
               "StandardOutPath": log, "StandardErrorPath": log,
               "RunAtLoad": True,
               "KeepAlive": {"SuccessfulExit": False},
               "ExitTimeOut": STOP_TIMEOUT,
               "ProcessType": "Standard"}
        if self._domain() == "system":
            job["UserName"] = self._tools["runner_user"]
        return plistlib.dumps(job).decode("utf-8")

    def _load(self, rid):
        """Bootstrap the job unless launchd already has it."""
        if self._loaded(rid):
            return
        record = self.adopted(rid)
        if record and not record.get("plist"):
            raise RuntimeError(
                f"the adopted job {record['label']!r} is not loaded and this "
                f"runtime does not know where its definition lives; adopt it "
                f"again naming its plist, or recreate it as an ordinary "
                f"instance")
        plist = (record or {}).get("plist") or self.paths(rid)["plist"]
        ok, out, err = self._launchctl("bootstrap", self._domain(), plist)
        if not ok and "already" not in (out + err).lower():
            raise RuntimeError(err or out or "launchctl bootstrap failed")

    def _loaded(self, rid):
        ok, out, err = self._launchctl("print", self._target(rid))
        if ok:
            return True
        if _not_found(out + err):
            return False
        raise RuntimeError(err or out or "launchctl print failed")

    def start(self, runner_id):
        rid = naming.check(runner_id)
        self._appliance_up()
        self._check(self._launchctl("enable", self._target(rid)))
        self._load(rid)
        self._check(self._launchctl("kickstart", self._target(rid)))

    def stop(self, runner_id):
        """Unloaded, so launchd does not start it again. SIGTERM first, and
        the ExitTimeOut grace a deregistration needs."""
        rid = naming.check(runner_id)
        self._check(self._launchctl("disable", self._target(rid)))
        ok, out, err = self._launchctl("bootout", self._target(rid),
                                       timeout=STOP_TIMEOUT + 30)
        if not ok and not _not_found(out + err):
            raise RuntimeError(err or out or "launchctl bootout failed")

    def restart(self, runner_id):
        rid = naming.check(runner_id)
        self._appliance_up()
        self._check(self._launchctl("enable", self._target(rid)))
        self._load(rid)
        self._check(self._launchctl("kickstart", "-k", self._target(rid),
                                    timeout=STOP_TIMEOUT + 30))

    def drain(self, runner_id):
        """A graceful stop that stays stopped (OPEN-7): SIGTERM to the job's
        process. The runner takes nothing new, finishes what it has and
        exits cleanly - and launchd restarts it only after an unclean exit
        (`KeepAlive: SuccessfulExit false`), so it stays down."""
        rid = naming.check(runner_id)
        record = self.adopted(rid)
        path = (record or {}).get("plist") or self.paths(rid)["plist"]
        try:
            definition = plistlib.loads(self._fs.read_text(path).encode())
        except (OSError, ValueError, TypeError):
            raise RuntimeError("cannot verify launchd drain policy; migrate this runner first")
        if definition.get("KeepAlive") not in (False, None, {"SuccessfulExit": False}):
            raise RuntimeError("unsafe launchd KeepAlive policy; migrate this runner before draining")
        self._check(self._launchctl("disable", self._target(rid)))
        if self.status(rid).get("running"):
            self._check(self._launchctl("kill", "SIGTERM", self._target(rid)))

    def cancel_drain(self, runner_id):
        """Back into service once drained: loaded and started again."""
        rid = naming.check(runner_id)
        self._check(self._launchctl("enable", self._target(rid)))
        self._load(rid)
        if not self.status(rid).get("running"):
            self._check(self._launchctl("kickstart", self._target(rid)))

    def remove(self, runner_id, keep_data):
        """Remove the job and its storage. Safe when any of it is absent."""
        rid = naming.check(runner_id)
        self.stop(rid)
        p = self.paths(rid)
        if self._domain() == "system":
            self._check(self._run(["/usr/bin/sudo", "-n", "/bin/rm", "-f",
                                  "--", p["plist"]]))
            # Root's hooks, by the same privileged rm; their empty directory
            # stays, as rm -f does not remove one.
            hooks = self._hooks_dir(p, rid)
            self._check(self._run(["/usr/bin/sudo", "-n", "/bin/rm", "-f", "--",
                                   *[posixpath.join(hooks, n) for n in HOOK_FILES]]))
        else:
            self._fs.remove(p["plist"])
        doomed = ([p[a] for a in AREAS if a not in KEPT_ON_RECREATE]
                  + [p["tmp"]]) if keep_data else [p["root"]]
        left = []
        for path in doomed:
            try:
                self._fs.rmtree(path)
            except OSError as e:
                left.append(f"{posixpath.basename(path)}: {e}")
        if left:
            raise RuntimeError("storage left behind: " + "; ".join(left))

    def _check(self, result):
        ok, out, err = result
        if not ok:
            raise RuntimeError(err or out or "failed")

    # ---- observation ---------------------------------------------------------

    def status(self, runner_id):
        """Whether the instance exists and runs, and what it was made from.
        `exists` is None - unknown - when launchd could not be asked."""
        rid = naming.check(runner_id)
        p = self.paths(rid)
        record = self.adopted(rid)
        if not record and not self._fs.exists(p["plist"]):
            return {"exists": False, "running": False, "state": "absent"}
        ok, out, err = self._launchctl("print", self._target(rid))
        template = record.get("template") if record else self._template(p)
        if not ok:
            if _not_found(out + err):
                return {"exists": True, "running": False, "state": "exited",
                        "runtime_template": template}
            return {"exists": None, "running": None, "state": "unknown"}
        state = _field(out, "state") or "unknown"
        pid = _int(_field(out, "pid"))
        running = True if state == "running" or pid else (
            False if state in ("waiting", "not running", "exited") else None)
        return {"exists": True, "running": running,
                "state": "running" if running is True else (
                    "exited" if running is False else "unknown"),
                "pid": pid,
                "runtime_template": template}

    def _template(self, p):
        try:
            return self._fs.read_text(posixpath.join(p["reg"],
                                                     TEMPLATE_MARKER)).strip()
        except OSError:
            return None

    def telemetry(self, runner_id):
        """This instance's processes; the guest's cores and memory, which is
        what an appliance runner - bound by no limit of its own - shares;
        and the volume its tree is on. That is the Data volume, read where
        the runner lives: `/` on APFS is the sealed system volume, and its
        figures say nothing about the disk that fills."""
        result = {"cpu_percent": None, "mem_used_bytes": None,
                  "mem_limit_bytes": None, "cpu_cores": None,
                  "host_cores": None, "host_mem_bytes": None,
                  "storage_volume_used_bytes": None,
                  "storage_volume_total_bytes": None}
        # Named, not `-n`: sysctl answers the names it knows and fails the
        # call for one it does not, so each figure is read by its own name
        # whatever the exit status - one it cannot read loses only itself.
        _, out, _ = self._run(["/usr/sbin/sysctl", "hw.logicalcpu",
                               "hw.memsize"], timeout=5)
        named = {}
        for line in (out or "").splitlines():
            name, sep, value = line.partition(":")
            if sep:
                named[name.strip()] = value.strip()
        result["host_cores"] = _int(named.get("hw.logicalcpu"))
        result["host_mem_bytes"] = _int(named.get("hw.memsize"))
        pid = self.status(runner_id).get("pid")
        if pid:
            ok, out, _ = self._run([self._tools["ps"], "-A", "-o",
                                    "pid=,ppid=,pgid=,%cpu=,rss="], timeout=15)
            if ok:
                processes = {}
                for line in out.splitlines():
                    parts = line.split()
                    if len(parts) == 5:
                        try:
                            processes[int(parts[0])] = (int(parts[1]), int(parts[2]),
                                                         float(parts[3]), int(parts[4]) * 1024)
                        except (ValueError, OverflowError):
                            continue
                owned = {pid} | {p for p, (_, group, _, _) in processes.items() if group == pid}
                while True:
                    descendants = {p for p, (parent, _, _, _) in processes.items() if parent in owned}
                    if descendants <= owned:
                        break
                    owned.update(descendants)
                measured = [processes[p] for p in owned if p in processes]
                if measured:
                    result["cpu_percent"] = round(sum(p[2] for p in measured), 2)
                    result["mem_used_bytes"] = sum(p[3] for p in measured)
        volume = self._volume(self.paths(runner_id)["root"])
        result["storage_volume_used_bytes"] = volume["volume_used_bytes"]
        result["storage_volume_total_bytes"] = volume["volume_total_bytes"]
        return result

    def _volume(self, path):
        """The volume `path` is on, which every appliance runner shares."""
        try:
            usage = self._fs.disk_usage(path) or {}
        except OSError:
            usage = {}
        return {"volume_used_bytes": usage.get("used_bytes"),
                "volume_total_bytes": usage.get("total_bytes")}

    def jobs(self, runner_ids):
        return {rid: current_job(self.logs(rid, 86400)) for rid in runner_ids}

    def logs(self, runner_id, since_seconds, max_bytes=256 * 1024):
        rid = naming.check(runner_id)
        paths = []
        adopted = self.adopted(rid)
        if adopted and adopted.get("plist"):
            try:
                plist = plistlib.loads(self._fs.read_text(
                    adopted["plist"]).encode("utf-8"))
                for key in ("StandardErrorPath", "StandardOutPath"):
                    path = plist.get(key)
                    if isinstance(path, str) and posixpath.isabs(path):
                        paths.append(path)
            except (OSError, ValueError, TypeError):
                pass
        paths.append(posixpath.join(self.paths(rid)["logs"], "runner.log"))
        for path in dict.fromkeys(paths):
            try:
                if time.time() - self._fs.mtime(path) <= since_seconds:
                    text = self._fs.tail(path, max_bytes)
                    if text:
                        return text
            except OSError:
                continue
        return ""

    def probe(self, runner_id, probe):
        p = self.paths(runner_id)
        volume = {}
        if probe == "disk_usage":
            value = self._fs.du(p["root"])
            volume = self._volume(p["root"])
        elif probe == "cache_size":
            value = self._fs.du(p["cache"])
            volume = self._volume(p["cache"])
        elif probe == "agent_version":
            try:
                value = self._fs.read_text(
                    posixpath.join(p["reg"], "agent_version")).strip()
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
        """Every runner instance in this appliance, and its state.

        An instance this runtime made has a job whose name says which runner
        it is. An adopted one has the job it always had, under a name that
        says nothing (T-0802), so the record written at adoption answers for
        it - without which the runner appears in no heartbeat at all."""
        found, seen = [], set()
        for name in self._fs.listdir(naming.MACOS_ROOT):
            try:
                rid = naming.check(name)
            except naming.InvalidRunnerId:
                continue
            if not self.adopted(rid):
                continue
            state = self.status(rid)
            seen.add(rid)
            found.append({"runner_id": rid,
                          "state": "running" if state.get("running") is True else
                          ("stopped" if state.get("running") is False else "unknown")})
        prefix = LABEL_PREFIX + naming.PREFIX + "-"
        for name in self._fs.listdir(self._tools["launch_agents"]):
            if not (name.startswith(prefix) and name.endswith(".plist")):
                continue
            rid = name[len(prefix):-len(".plist")]
            try:
                naming.check(rid)
            except naming.InvalidRunnerId:
                continue
            if rid in seen:
                continue
            s = self.status(rid)
            found.append({"runner_id": rid,
                          "state": "running" if s.get("running") is True else
                          ("stopped" if s.get("running") is False else "unknown")})
        return found

    # ---- cache ---------------------------------------------------------------

    def clear_cache(self, runner_id, policy):
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
            return sorted(name for name in self._fs.listdir(self._tools["templates"])
                          if name and posixpath.basename(name) == name
                          and not name.startswith(".") and all(self._fs.exists(
                              posixpath.join(self._tools["templates"], name, entry))
                              for entry in ("run", "register", "deregister")))
        except OSError:
            return []

    def capabilities(self):
        return {"kind": self.kind,
                "builds_from": "template",
                "templates": self._templates(),
                "appliance_control": self._appliance is not None,
                # A macOS guest runs no job images (design 9.5).
                "job_containers": False,
                "nested_builds": False,
                # The appliance can be reset to a snapshot in principle
                # (design 9.5, option B); nothing here does it yet.
                "resettable_os": False,
                # OPEN-7: a graceful stop that stays stopped.
                "supports_drain": True,
                "clear_cache": True,
                "cache_scopes": sorted(SUPPORTED_SCOPES),
                "notes": "one launchd job per runner inside the macOS "
                         "appliance, over a directory tree only its user "
                         "can read; no per-instance memory or CPU cap - the "
                         "appliance's own size is the limit"}


class MacRegistrar:
    """Registers the runner through its template's `register`, with the plan -
    token included - on standard input, never in an argument list."""

    def __init__(self, run=None, fs=None):
        self._run = run or _exec
        self._fs = fs or LocalFs()

    def _entry(self, runner_id, name):
        reg = naming.names(naming.check(runner_id), "macos")["reg"]
        return posixpath.join(reg, name)

    def register(self, runner_id, plan):
        entry = self._entry(runner_id, "register")
        if not self._fs.exists(entry):
            raise RuntimeError("the instance has no registration entry point")
        ok, out, err = self._run([entry], input=json.dumps(plan), timeout=120)
        if not ok:
            raise RuntimeError(err or "registration failed")
        try:
            answer = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise RuntimeError("the instance did not say how it was "
                               "registered")
        return {"registration_id": str(answer.get("registration_id") or ""),
                "registration_uuid": answer.get("registration_uuid")}

    def deregister(self, runner_id):
        entry = self._entry(runner_id, "deregister")
        if not self._fs.exists(entry):
            raise RuntimeError("the instance is gone; its registration can "
                               "only be removed at the forge")
        ok, out, err = self._run([entry], timeout=60)
        if not ok:
            raise RuntimeError(err or out or "deregistration failed")


def _serves_github(spec):
    """Whether the controller made this unit for GitHub, by its label."""
    return ((spec or {}).get("labels") or {}).get("nomercy.provider") == "github"


def _not_found(text):
    """launchd's ways of saying it has no such job."""
    text = text.lower()
    return ("could not find service" in text or "no such process" in text
            or "113" in text)


def _field(text, name):
    for line in text.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep and key == name:
            return value.strip()
    return None


def _int(text):
    try:
        return int(text)
    except (TypeError, ValueError):
        return None
