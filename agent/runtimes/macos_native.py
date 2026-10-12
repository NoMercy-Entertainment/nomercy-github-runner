"""Pinned native payloads for a fresh owned guest; never alters its backing disk."""
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import tempfile

from .guest_ssh import GuestExec

STREAM = "/Library/NoMercy/job-events/job_event_stream.py"
BINARY_HASHES = {
    "amd64": "14f418e54dcb78df96f1b0203299b25be349dedf8cfdf36fa955bef53674c1c3",
    "arm64": "0c66df9e5fe1f81b8461ccaee7a272840274149ed7201d87d7cb8fa76795b0bc",
}
MAX_BINARY = 32 * 1024**2


def protected_directory(path):
    path = Path(path)
    if not path.is_absolute():
        raise RuntimeError("native provisioning control path must be absolute")
    for parent in reversed((path, *path.parents)):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("native provisioning control ancestor is not protected")


def protected_read(path, maximum):
    """Read only bounded root-owned files beneath protected, non-link ancestors."""
    path = Path(path)
    protected_directory(path.parent)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022
                or before.st_nlink != 1 or before.st_size > maximum):
            raise RuntimeError("native provisioning control file is not protected and bounded")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            data = source.read(maximum + 1)
        after = os.fstat(descriptor)
        fields = ("st_dev", "st_ino", "st_mode", "st_uid", "st_gid", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")
        if len(data) > maximum or any(getattr(before, key) != getattr(after, key) for key in fields):
            raise RuntimeError("native provisioning control file changed while reading")
        return data
    finally:
        os.close(descriptor)


def host_key(value):
    if not isinstance(value, str) or len(value) > 256 or len(value.split()) != 2:
        raise RuntimeError("trusted base SSH public key must be one Ed25519 key")
    kind, encoded = value.split()
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError:
        raise RuntimeError("trusted base SSH public key is malformed") from None
    if kind != "ssh-ed25519" or decoded != struct.pack(">I", 11) + b"ssh-ed25519" + struct.pack(">I", 32) + decoded[-32:] or len(decoded) != 51:
        raise RuntimeError("trusted base SSH public key is malformed")
    return kind + " " + encoded


def base_identity(path):
    info = Path(path).lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("trusted native base is not a protected root-owned disk")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)]


class PinnedGuestExec(GuestExec):
    def __init__(self, *, known_hosts, **kwargs):
        super().__init__(**kwargs)
        self.known_hosts = str(known_hosts)

    def _options(self):
        pin = protected_read(self.known_hosts, 4096).decode("ascii")
        prefix = "[127.0.0.1]:" + str(self.port) + " "
        if not pin.endswith("\n") or not pin.startswith(prefix) or "\n" in pin[:-1]:
            raise RuntimeError("owned guest SSH pin names another endpoint")
        host_key(pin[len(prefix):-1])
        original, filtered, index = super()._options(), [], 0
        while index < len(original):
            if original[index] == "-o" and original[index + 1].startswith((
                    "StrictHostKeyChecking=", "UserKnownHostsFile=", "ControlMaster=", "ControlPath=", "ControlPersist=")):
                index += 2
            else:
                filtered.append(original[index])
                index += 1
        return [*filtered, "-o", "StrictHostKeyChecking=yes", "-o", "UserKnownHostsFile=" + self.known_hosts,
                "-o", "GlobalKnownHostsFile=/dev/null", "-o", "ControlMaster=no", "-o", "ControlPath=none"]


# Literal management code, passed directly to the OS Python. No guest-staged
# executable or runner-controlled module is imported by sudo.
INSTALL = r'''
import base64,hashlib,json,os,pwd,stat,sys,uuid
value=json.loads(sys.stdin.read(48*1024*1024+1))
assert value['action'] in ('install','verify')
assert value['architecture'] in ('amd64','arm64')
assert os.uname().machine == {'amd64':'x86_64','arm64':'arm64'}[value['architecture']]
rid=value['runner_id'];assert str(uuid.UUID(rid))==rid
reg='/Users/runner/runners/'+rid+'/reg'
uid=pwd.getpwnam(value['runner_user']).pw_uid
files=value['files'];assert set(files)<= {'/Library/NoMercy/job-events/job_event_stream.py',reg+'/forgejo-runner'}
assert '/Library/NoMercy/job-events/job_event_stream.py' in files
result={}
for target,payload in files.items():
 protected=target.startswith('/Library/')
 data=None
 if value['action']=='install':
  data=base64.b64decode(payload['data'],validate=True)
  assert len(data)<=32*1024*1024 and hashlib.sha256(data).hexdigest()==payload['sha256']
 assert payload['mode']==(0o644 if protected else 0o755)
 descriptors=[];fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);descriptors.append(fd)
 try:
  for name in target.split('/')[1:-1]:
   try:next_fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
   except FileNotFoundError:
    assert protected and value['action']=='install'
    os.mkdir(name,0o755,dir_fd=fd)
    next_fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
   info=os.fstat(next_fd)
   assert info.st_uid in ((0,) if protected else (0,uid))
   if protected:assert not info.st_mode&0o022
   descriptors.append(next_fd);fd=next_fd
  leaf=target.rsplit('/',1)[1]
  try:
   old=os.stat(leaf,dir_fd=fd,follow_symlinks=False)
   assert stat.S_ISREG(old.st_mode) and old.st_nlink==1 and old.st_uid in (0,uid)
  except FileNotFoundError:pass
  if value['action']=='install':
   temporary='.native-'+uuid.uuid4().hex
   out=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=fd)
   try:
    with os.fdopen(out,'wb',closefd=False) as stream:stream.write(data);stream.flush();os.fsync(out)
    os.fchown(out,0,0);os.fchmod(out,payload['mode']);os.fsync(out)
    os.replace(temporary,leaf,src_dir_fd=fd,dst_dir_fd=fd)
    os.fsync(fd)
   finally:
    os.close(out)
    try:os.unlink(temporary,dir_fd=fd)
    except FileNotFoundError:pass
  check=os.open(leaf,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=fd)
  try:
   info=os.fstat(check)
   with os.fdopen(check,'rb',closefd=False) as stream:installed=stream.read(32*1024*1024+1)
   assert info.st_uid==0 and info.st_nlink==1 and stat.S_IMODE(info.st_mode)==payload['mode']
   assert len(installed)<=32*1024*1024 and hashlib.sha256(installed).hexdigest()==payload['sha256']
   if data is not None:assert installed==data
  finally:os.close(check)
  result[target]=payload['sha256']
 finally:
  for descriptor in reversed(descriptors):os.close(descriptor)
print(json.dumps({'installed':result}))
'''


class NativeProvisioner:
    def __init__(self, base_disk, image):
        # The operator attests this exact configured base; never learns a
        # trusted key from an unauthenticated scan or an arbitrary old guest.
        self.base = Path(base_disk)
        manifest = self.base.with_name(self.base.name + ".native.json")
        try:
            value = json.loads(protected_read(manifest, 65536))
        except FileNotFoundError:
            raise RuntimeError("fresh macOS creation requires protected base .native.json SSH-key and artifact attestation") from None
        if (not isinstance(value, dict) or type(value.get("version")) is not int or value["version"] != 1
                or value.get("image") != image or value.get("base_disk") != str(self.base)
                or value.get("base_identity") != base_identity(self.base) or value.get("architecture") not in BINARY_HASHES
                or not isinstance(value.get("artifact_root"), str) or not Path(value["artifact_root"]).is_absolute()):
            raise RuntimeError("native base attestation does not match this configured image and disk")
        self.identity = value["base_identity"]
        self.key = host_key(value.get("host_key"))
        self.architecture = value["architecture"]
        self.stream = protected_read(Path(__file__).resolve().parents[1] / "job_event_stream.py", 1024**2)
        artifact = Path(value["artifact_root"]) / ("forgejo-runner-v13.1.0-darwin-" + self.architecture)
        self.binary = protected_read(artifact, MAX_BINARY)
        if hashlib.sha256(self.binary).hexdigest() != BINARY_HASHES[self.architecture]:
            raise RuntimeError("native Forgejo artifact does not match the pinned v2 release")

    def pin(self, record, directory):
        if base_identity(self.base) != self.identity:
            raise RuntimeError("trusted native base disk identity changed")
        path = Path(directory) / "guest_known_hosts"
        protected_directory(directory)
        data = ("[127.0.0.1]:" + str(record["port"]) + " " + self.key + "\n").encode("ascii")
        if path.exists():
            if protected_read(path, 4096) != data:
                raise RuntimeError("existing guest SSH pin differs from the trusted base")
            return
        descriptor, temporary = tempfile.mkstemp(prefix=".native-pin-", dir=directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary, path)
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if os.path.exists(temporary):os.unlink(temporary)

    def _apply(self, guest, record, paths, runner_user, action):
        if base_identity(self.base) != self.identity:
            raise RuntimeError("trusted native base disk identity changed")
        expected_reg = "/Users/runner/runners/" + record["runner_id"] + "/reg"
        if paths["reg"] != expected_reg or not runner_user:
            raise RuntimeError("native installation requires the owned guest registration and account")
        files = {STREAM: (self.stream, 0o644)}
        if record["template"].startswith("forgejo-"):
            files[expected_reg + "/forgejo-runner"] = (self.binary, 0o755)
        payload = {"action": action, "runner_id": record["runner_id"], "runner_user": runner_user,
                   "architecture": self.architecture,
                   "files": {path: {"sha256": hashlib.sha256(data).hexdigest(), "mode": mode,
                       **({"data": base64.b64encode(data).decode("ascii")} if action == "install" else {})}
                       for path, (data, mode) in files.items()}}
        wanted = {path: value["sha256"] for path, value in payload["files"].items()}
        if action == "verify" and record.get("native_files") != wanted:
            raise RuntimeError("prepared native file identity differs from the protected SDK payload")
        ok, out, _ = guest(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-B", "-c", INSTALL],
                           input=json.dumps(payload), timeout=300)
        try:
            valid = ok and json.loads(out) == {"installed": wanted}
        except ValueError:
            valid = False
        if not valid:
            raise RuntimeError("fresh guest native installation failed; listener remains disabled")
        return wanted

    def install(self, guest, record, paths, runner_user):
        return self._apply(guest, record, paths, runner_user, "install")

    def verify(self, guest, record, paths, runner_user):
        return self._apply(guest, record, paths, runner_user, "verify")
