"""Install the reviewed Forgejo guard outside every job-writable directory."""
import base64
import hashlib
import json
from pathlib import Path
import stat

from .. import naming

ENVIRONMENT = "NOMERCY_FORGEJO_ADMISSION_POLICY"
DIGEST = "76ed71522d4fd6c16287c71db0fa5a5020832c7bf339bc70fc8bd3ebcf23b9c4"

INSTALL = r'''
import base64,hashlib,json,os,stat,sys,tempfile
from pathlib import Path
assert os.geteuid()==0
body=json.loads(sys.stdin.read(90*1024**2+1))
rid=body['runner_id']
import re
assert re.fullmatch(r'[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}',rid)
root=Path('/Library/NoMercy/forgejo-admission')/rid
for parent in reversed((root,*root.parents)):
 if not parent.exists():parent.mkdir(mode=0o755)
 info=parent.lstat()
 assert stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and info.st_uid==0 and not info.st_mode&0o022
binary=base64.b64decode(body['binary'],validate=True)
assert len(binary)<=64*1024**2 and hashlib.sha256(binary).hexdigest()==body['sha256']
for name,data in [('forgejo-runner',binary),('run',body['launcher'].encode())]:
 path=root/name
 if path.exists():
  info=path.lstat();assert stat.S_ISREG(info.st_mode) and info.st_uid==0 and info.st_nlink==1 and not info.st_mode&0o022
 fd,tmp=tempfile.mkstemp(prefix='.install-',dir=root)
 try:
  with os.fdopen(fd,'wb') as file:file.write(data);file.flush();os.fsync(file.fileno())
  os.chmod(tmp,0o755);os.replace(tmp,path)
 finally:
  if os.path.exists(tmp):os.unlink(tmp)
print(json.dumps({'installed':True,'sha256':hashlib.sha256(binary).hexdigest()}))
'''

LAUNCHER = '''#!/usr/bin/python3
import json,os,signal,sys,time
from pathlib import Path
root=Path(REG)
stopping=False
def stop(*args):
 global stopping
 stopping=True
signal.signal(signal.SIGTERM,stop)
signal.signal(signal.SIGINT,stop)
while not (root/'.runner').exists():
 if stopping:sys.exit(0)
 time.sleep(1)
if stopping:sys.exit(0)
config={'runner':{'file':str(root/'.runner'),'capacity':1,
 'timeout':'2562047h47m16s','shutdown_timeout':'2562047h47m16s'},
 'cache':{'dir':str(root.parent/'cache'/'actcache')},
 'host':{'workdir_parent':str(root.parent/'work')}}
(root/'config.yaml').write_text(json.dumps(config))
os.chdir(root)
os.execv(BINARY,[BINARY,'daemon','--config',str(root/'config.yaml')])
'''


def program(rid):
    return "/Library/NoMercy/forgejo-admission/" + naming.check(rid) + "/run"


def install(runtime, rid):
    artifact = runtime._tools.get("forgejo_admission_binary")
    if not artifact:
        raise RuntimeError("Forgejo admission needs a reviewed protected artifact")
    artifact = Path(artifact)
    for path in (artifact, *artifact.parents):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError("Forgejo admission artifact is not protected")
    if not artifact.is_file() or artifact.stat().st_nlink != 1 or artifact.stat().st_size > 64*1024**2:
        raise RuntimeError("invalid Forgejo admission artifact")
    binary = artifact.read_bytes()
    if hashlib.sha256(binary).hexdigest() != DIGEST:
        raise RuntimeError("Forgejo admission artifact differs from its reviewed build")
    p = runtime.paths(rid)
    executable = program(rid).removesuffix("/run") + "/forgejo-runner"
    launcher = LAUNCHER.replace("Path(REG)", "Path(" + repr(p['reg']) + ")")
    launcher = launcher.replace("BINARY", repr(executable))
    compile(launcher, program(rid), "exec")
    payload = dict(runner_id=rid, binary=base64.b64encode(binary).decode(), sha256=DIGEST, launcher=launcher)
    ok, out, error = runtime._run(["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "-c", INSTALL],
                                  input=json.dumps(payload), timeout=300)
    if not ok or json.loads(out).get('sha256') != DIGEST:
        raise RuntimeError("protected Forgejo admission installation failed: " + str(error))
    return program(rid)
