"""The guest of an appliance, reached over SSH from the appliance host.

`macos_appliance.py` takes a `run` and an `fs`; on a Mac that is the machine
the agent runs on. The appliance here is a QEMU guest whose only way in is
the SSH port its host forwards, and whose hypervisor side - power, the boot
leftover of design 9.3.1 - can only be reached from that host. So the agent
runs on the host, which is the worker kind the state store already names
`macos-appliance-host`, and this module is the two arguments that make the
runtime act inside the guest instead of on the host's own disk.

Every method is one command over one multiplexed connection: `ControlMaster`
keeps a single SSH session open, so a telemetry sweep is one handshake and
not a dozen. The commands are the tools macOS has - `stat -f`, `du -sk` -
because the guest is BSD userland, not GNU.

Two rules this module keeps:

- **Arguments are quoted, always.** The runtime hands over argv; a remote
  shell gets one string. A path with a space is one argument here too.
- **A secret never reaches a command line.** A password goes to `sshpass -e`
  through the environment, and file content goes over standard input, so
  neither shows up in `ps` on the appliance host.

Nothing here decides where anything goes: the runtime derives every path.
"""
import os
import hashlib
import shlex
import subprocess
import tempfile

from .localfs import df_figures

#: Long enough that a sweep of a dozen calls shares one connection, short
#: enough that a dead guest is not held open.
CONTROL_PERSIST = "60s"


def _run(argv, input=None, timeout=None, env=None):
    done = subprocess.run([*argv], input=input, text=True,
                          capture_output=True, timeout=timeout,
                          env=dict(os.environ, **(env or {})))
    return done.returncode, done.stdout, done.stderr


class GuestExec:
    """Runs argv inside the guest. Answers `(ok, stdout, stderr)`, the shape
    every runtime in this package expects, and never raises: a guest that is
    down is a failed call, not a crashed agent."""

    def __init__(self, host, user, port=22, key=None, password=None,
                 ssh="ssh", sshpass="sshpass", control_dir=None, runner=None):
        self.host = host
        self.user = user
        self.port = int(port)
        self.key = key
        self._password = password
        self._ssh = ssh
        self._sshpass = sshpass
        identity = "%s@%s:%d" % (user, host, self.port)
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        self._control = control_dir or os.path.join(
            tempfile.gettempdir(), "agent-guest-%s.sock" % digest)
        self._runner = runner or _run

    def __repr__(self):
        return "GuestExec(%s@%s:%d)" % (self.user, self.host, self.port)

    def _options(self):
        options = ["-p", str(self.port),
                   "-o", "StrictHostKeyChecking=no",
                   "-o", "UserKnownHostsFile=/dev/null",
                   "-o", "LogLevel=ERROR",
                   "-o", "ConnectTimeout=10",
                   "-o", "ControlMaster=auto",
                   "-o", "ControlPath=" + self._control,
                   "-o", "ControlPersist=" + CONTROL_PERSIST]
        if self.key:
            options += ["-i", self.key, "-o", "IdentitiesOnly=yes"]
        if self._password:
            # sshpass answers the prompt; asking a human is still refused.
            options += ["-o", "PubkeyAuthentication=no",
                        "-o", "PreferredAuthentications=password"]
        else:
            options += ["-o", "BatchMode=yes"]
        return options

    def __call__(self, args, input=None, timeout=30):
        remote = " ".join(shlex.quote(str(a)) for a in args)
        argv = [self._ssh, *self._options(),
                "%s@%s" % (self.user, self.host), remote]
        env = {}
        if self._password:
            argv = [self._sshpass, "-e", *argv]
            env["SSHPASS"] = self._password
        try:
            code, out, err = self._runner(argv, input=input, timeout=timeout,
                                          env=env)
        except subprocess.TimeoutExpired:
            return False, "", "no answer from the guest within %ss" % timeout
        except OSError as e:
            return False, "", str(e)
        if "mux_client_request_session: session request failed" in err:
            # SSH has already executed the command through its fallback.
            # Retire the refusing master without closing its active sessions
            # or executing a potentially destructive command twice.
            try:
                self._runner([self._ssh, *self._options(), "-O", "stop",
                              "%s@%s" % (self.user, self.host)], timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        return code == 0, out, err


class GuestFs:
    """The `fs` of `macos_appliance`, acting inside the guest.

    The same small interface as `LocalFs`, so the runtime cannot tell the
    difference and the contract suite holds both to the same behaviour."""

    def __init__(self, exec_):
        self._exec = exec_

    def _sh(self, script, input=None, timeout=30):
        """One `sh -c` inside the guest. The script is a single argument, so
        the runtime's paths are quoted into it by the caller."""
        return self._exec(["/bin/sh", "-c", script], input=input,
                          timeout=timeout)

    def exists(self, path):
        ok, out, err = self._sh("if test -e %s; then printf 'present'; "
                                "else printf 'absent'; fi" % shlex.quote(path))
        if not ok or out.strip() not in ("present", "absent"):
            raise OSError("could not observe %s in the guest: %s" % (path, err))
        return out.strip() == "present"

    def makedirs(self, path):
        ok, _, err = self._sh("mkdir -p %s" % shlex.quote(path))
        if not ok:
            raise OSError("could not make %s in the guest: %s" % (path, err))

    def chmod(self, path, mode):
        ok, _, err = self._sh("chmod %o %s" % (mode, shlex.quote(path)))
        if not ok:
            raise OSError("could not chmod %s in the guest: %s" % (path, err))

    def listdir(self, path):
        quoted = shlex.quote(path)
        ok, out, err = self._sh("if test -d %s; then ls -1A %s; fi" % (quoted, quoted))
        if not ok:
            raise OSError("could not list %s in the guest: %s" % (path, err))
        return sorted(line for line in out.splitlines() if line)

    def copytree(self, src, dst):
        quoted_src, quoted_dst = shlex.quote(src + "/."), shlex.quote(dst)
        ok, _, err = self._sh("mkdir -p %s && cp -R %s %s"
                              % (quoted_dst, quoted_src, quoted_dst),
                              timeout=300)
        if not ok:
            raise OSError("could not copy %s to %s in the guest: %s"
                          % (src, dst, err))

    def write_text(self, path, text, mode=None):
        """Atomically, and private before it has content: the temporary file
        is created and chmod-ed first, then filled, then moved into place."""
        target, tmp = shlex.quote(path), shlex.quote(path + ".tmp")
        steps = [": > %s" % tmp]
        if mode is not None:
            steps.append("chmod %o %s" % (mode, tmp))
        steps.append("cat > %s" % tmp)
        steps.append("mv %s %s" % (tmp, target))
        ok, _, err = self._sh(" && ".join(steps), input=text)
        if not ok:
            raise OSError("could not write %s in the guest: %s" % (path, err))

    def read_text(self, path):
        ok, out, err = self._sh("cat %s" % shlex.quote(path))
        if not ok:
            raise OSError("could not read %s in the guest: %s" % (path, err))
        return out

    def tail(self, path, max_bytes):
        ok, out, _ = self._sh("tail -c %d %s"
                              % (int(max_bytes), shlex.quote(path)))
        return out if ok else ""

    def mtime(self, path):
        ok, out, err = self._sh("stat -f %%m %s" % shlex.quote(path))
        if not ok:
            raise OSError("could not stat %s in the guest: %s" % (path, err))
        return float(out.strip())

    def remove(self, path):
        ok, _, err = self._sh("rm -f %s" % shlex.quote(path))
        if not ok:
            raise OSError("could not remove %s in the guest: %s"
                          % (path, err))

    def rmtree(self, path):
        ok, _, err = self._sh("rm -rf %s" % shlex.quote(path), timeout=300)
        if not ok:
            raise OSError("could not remove %s in the guest: %s"
                          % (path, err))

    def clear_dir(self, path):
        """Delete what is inside `path`, keeping `path` - the same promise
        LocalFs makes, so a cache clear cannot take the directory a launchd
        job's environment points at."""
        quoted = shlex.quote(path)
        ok, _, err = self._sh(
            "test -d %s || exit 0; find %s -mindepth 1 -maxdepth 1 "
            "-exec rm -rf {} +" % (quoted, quoted), timeout=600)
        if not ok:
            raise OSError("could not clear %s in the guest: %s" % (path, err))

    def du(self, path):
        """Bytes under `path`, or None when it cannot be read - never 0."""
        ok, out, _ = self._sh("du -sk %s" % shlex.quote(path), timeout=300)
        if not ok or not out.strip():
            return None
        try:
            return int(out.split()[0]) * 1024
        except (ValueError, IndexError):
            return None

    def disk_usage(self, path):
        """The volume holding `path` - on APFS the Data volume the runners
        live on, which `/`, the sealed system volume, is not - as
        {"used_bytes", "total_bytes"}, or None when it cannot be read."""
        ok, out, _ = self._sh("df -k %s" % shlex.quote(path), timeout=30)
        return df_figures(out) if ok else None
