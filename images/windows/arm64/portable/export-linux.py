"""Run as root on the source QEMU host, after a clean ARM guest shutdown."""
import datetime
import hashlib
import json
from pathlib import Path
import pwd
import shutil
import subprocess
import tarfile
import time

source = Path('/var/lib/runner-appliances/windows-arm64')
stage = Path('/tmp/nomercy-arm-handoff-20261002')
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d-%H%M%S')
destination = source / ('handoff-' + stamp)
package = destination / 'NoMercy-Windows-ARM64-M4'
destination.mkdir(mode=0o700)
result = destination / 'export-result.json'

def status(phase, **details):
    data = {'phase': phase, 'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), **details}
    result.write_text(json.dumps(data, indent=2))
    print(json.dumps(data), flush=True)

def run(args):
    subprocess.run([str(a) for a in args], check=True)

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4*1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()

try:
    state = subprocess.check_output(['systemctl','show','-p','ActiveState','--value','windows-arm64-runner.service'],text=True).strip()
    assert state == 'inactive', 'Windows ARM must shut down cleanly before export'
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try:
            args=(proc/'cmdline').read_bytes()
            exe=(proc/'exe').resolve(strict=True).name
        except (FileNotFoundError,PermissionError,ProcessLookupError): continue
        assert not (exe=='qemu-system-aarch64' and b'windows-arm64-runner' in args), 'ARM QEMU is active'
        assert not (exe=='swtpm' and b'/var/lib/swtpm/windows-arm64-runner' in args), 'ARM TPM is active'
    assert shutil.disk_usage(source).free > 100*1024**3, 'Insufficient export space'
    with tarfile.open(stage/'scripts.tar') as archive:
        archive.extractall(package, filter='data')
    vm=package/'vm'; vm.mkdir()
    with (destination/'disk-copy.log').open('w') as log:
        for name in ('windows-arm64.qcow2','windows-arm64-runners.qcow2'):
            status('copying-disk', disk=name)
            subprocess.run(['qemu-img','convert','-c','-O','qcow2','-o','compression_type=zlib',str(source/name),str(vm/name)],check=True,stdout=log,stderr=log)
            subprocess.run(['qemu-img','check','-q',str(vm/name)],check=True,stdout=log,stderr=log)
            subprocess.run(['qemu-img','compare','-q',str(source/name),str(vm/name)],check=True,stdout=log,stderr=log)
            info=json.loads(subprocess.check_output(['qemu-img','info','--output=json',str(vm/name)],text=True))
            assert not info.get('backing-filename'), 'Export disk still has a backing file'
    shutil.copy2('/usr/share/AAVMF/AAVMF_CODE.no-secboot.fd',vm/'AAVMF_CODE.fd')
    shutil.copy2(source/'AAVMF_VARS.fd',vm/'AAVMF_VARS.fd')
    shutil.copytree('/var/lib/swtpm/windows-arm64-runner',vm/'tpm',ignore=shutil.ignore_patterns('*.sock','*.pid','*.lock'))
    status('hashing')
    manifest={'kind':'outbound','created_utc':stamp,'guest':'Windows 11 Pro ARM64','hardware_target':'Apple Silicon M4, 16 GB host RAM',
              'disk_copy_verified':True,'m4_boot_verified':False,'forge_registration':'not included; finish, test and return', 'files':{}}
    for file in sorted(package.rglob('*')):
        if file.is_file():
            if file.suffix=='.command':file.chmod(0o755)
            if file.name=='portable_ed25519':file.chmod(0o600)
            manifest['files'][str(file.relative_to(package))]=digest(file)
    (package/'manifest.json').write_text(json.dumps(manifest,indent=2))
    status('archiving')
    archive_path=destination/'NoMercy-Windows-ARM64-M4.tar'
    with tarfile.open(archive_path,'w') as archive:
        archive.add(package,arcname=package.name)
    checksum=digest(archive_path)
    checksum_path=archive_path.with_suffix('.tar.sha256')
    checksum_path.write_text(checksum+'  '+archive_path.name+'\n')
    owner=pwd.getpwnam('runner')
    for file in (destination,archive_path,checksum_path,result):
        shutil.chown(file,user=owner.pw_uid,group=owner.pw_gid)
    archive_path.chmod(0o600); checksum_path.chmod(0o600)
    status('ready', archive=str(archive_path), bytes=archive_path.stat().st_size, sha256=checksum)
except Exception as error:
    status('error', detail=str(error))
    raise
