#!/usr/bin/env python3
"""Launch on Apple Silicon, report guest preparation, and produce a return bundle."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time

ROOT = Path(__file__).resolve().parent
VM = ROOT / 'vm'
LOG = ROOT / 'logs'
SSH_PORT = 55322
VNC_PORT = 5908
RUNTIME = Path(tempfile.gettempdir()) / ('nm-arm-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12])
QMP = RUNTIME / 'qmp.sock'

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)

def qmp(command):
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(10)
        client.connect(str(QMP))
        stream = client.makefile('rwb')
        json.loads(stream.readline())
        for name in ('qmp_capabilities', command):
            stream.write(json.dumps({'execute': name}).encode() + b'\n')
            stream.flush()
            while True:
                response = json.loads(stream.readline())
                if 'error' in response:
                    raise RuntimeError(response['error'])
                if 'return' in response:
                    break
        return response['return']

def is_running():
    try:
        qmp('query-status')
        return True
    except (OSError, ValueError):
        return False

def ssh_args():
    key = ROOT / 'access' / 'portable_ed25519'
    key.chmod(0o600)
    return ['ssh', '-T', '-p', str(SSH_PORT), '-i', str(key),
            '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=nomercy-arm-portable',
            '-o', 'UserKnownHostsFile=' + str(ROOT / 'access' / 'known_hosts'),
            '-o', 'ConnectTimeout=10', 'admin@127.0.0.1']

def guest_status():
    command = ('C:/ProgramData/nomercy/agent/python/python.exe -c '
               '"import json; from pathlib import Path; p=Path(\'C:/ProgramData/nomercy/portable/status.json\'); '
               'print(p.read_text(encoding=\'utf-8-sig\') if p.exists() else json.dumps(dict(phase=\'starting\')))"')
    result = subprocess.run(ssh_args() + [command], capture_output=True, text=True, timeout=40)
    if result.returncode:
        return {'phase': 'booting', 'detail': 'Waiting for Windows SSH.'}
    return json.loads(result.stdout)

def collect_logs():
    LOG.mkdir(exist_ok=True)
    # Copy only preparation reports/logs, never account profiles or registration secrets.
    command = ('C:/ProgramData/nomercy/agent/python/python.exe -c '
               '"import pathlib,zipfile; r=pathlib.Path(\'C:/ProgramData/nomercy/portable\'); '
               'z=zipfile.ZipFile(\'C:/Users/admin/portable-reports.zip\',\'w\',zipfile.ZIP_DEFLATED); '
               '[z.write(p,p.name) for p in r.iterdir() if p.suffix in (\'.json\',\'.log\')]; z.close()"')
    run(ssh_args() + [command], timeout=60, stdout=subprocess.DEVNULL)
    args = ssh_args()
    scp = ['scp', '-P', str(SSH_PORT)] + args[4:-1]
    # ssh_args[4:] starts with -i; scp uses the same identity/host-key options.
    run(scp + ['admin@127.0.0.1:portable-reports.zip', str(LOG / 'guest-reports.zip')], timeout=120)

def stop():
    if is_running():
        print('Windows netjes afsluiten...', flush=True)
        # Guest shutdown also works when a debloat policy disables the ACPI button.
        try:
            result = subprocess.run(ssh_args() + ['shutdown.exe /s /t 0'],
                                    capture_output=True, text=True, timeout=40)
            if result.returncode and is_running():
                qmp('system_powerdown')
        except (OSError, subprocess.TimeoutExpired):
            if is_running():
                qmp('system_powerdown')
    deadline = time.monotonic() + 300
    while is_running():
        if time.monotonic() > deadline:
            raise RuntimeError('Windows sluit niet af. Sluit Windows via het scherm af; er wordt niet geforceerd gestopt.')
        time.sleep(5)
    # Wait for the process to release the disk and TPM files.
    pidfile = RUNTIME / 'qemu.pid'
    if pidfile.exists():
        pid = int(pidfile.read_text())
        while True:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            if time.monotonic() > deadline:
                raise RuntimeError('QEMU is nog actief; export is niet veilig.')
            time.sleep(2)
    tpm_pid = RUNTIME / 'swtpm.pid'
    if tpm_pid.exists():
        pid = int(tpm_pid.read_text())
        # Restrict the signal to this package's swtpm process, verified by its socket path.
        description = subprocess.run(['ps', '-p', str(pid), '-o', 'command='], capture_output=True, text=True).stdout
        if str(RUNTIME / 'tpm.sock') in description and 'swtpm' in description:
            os.kill(pid, 15)
            for _ in range(30):
                time.sleep(1)
                if subprocess.run(['kill', '-0', str(pid)], capture_output=True).returncode:
                    break
            else:
                raise RuntimeError('TPM is nog actief; export afgebroken.')

def export_return():
    if is_running():
        collect_logs()
    stop()
    name = 'NoMercy-Windows-ARM64-retour-' + time.strftime('%Y%m%d-%H%M%S')
    destination = ROOT.parent / name
    if shutil.disk_usage(ROOT.parent).free < 60 * 1024**3:
        raise RuntimeError('Voor de retourkopie is minimaal 60 GB vrije ruimte nodig.')
    destination.mkdir()
    for source in ROOT.iterdir():
        if source.name in ('vm', 'manifest.json', '.verified'):
            continue
        if source.is_dir():
            shutil.copytree(source, destination / source.name)
        else:
            shutil.copy2(source, destination / source.name)
    vmout = destination / 'vm'
    vmout.mkdir()
    for source in VM.iterdir():
        target = vmout / source.name
        if source.suffix == '.qcow2':
            print('Retourkopie controleren:', source.name, flush=True)
            run(['qemu-img', 'convert', '-c', '-O', 'qcow2', '-o', 'compression_type=zlib', source, target])
            run(['qemu-img', 'check', '-q', target])
            run(['qemu-img', 'compare', '-q', source, target])
        elif source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns('*.sock', '*.pid', '*.lock'))
        else:
            shutil.copy2(source, target)
    manifest = {'kind': 'return', 'created': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'files': {}}
    for file in sorted(destination.rglob('*')):
        if file.is_file():
            manifest['files'][str(file.relative_to(destination))] = digest(file)
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2))
    archive = destination.with_suffix('.tar')
    with tarfile.open(archive, 'w') as output:
        output.add(destination, arcname=name)
    checksum = digest(archive)
    archive.with_suffix('.tar.sha256').write_text(checksum + '  ' + archive.name + '\n')
    print('\nStuur deze twee bestanden terug:\n' + str(archive) + '\n' + str(archive.with_suffix('.tar.sha256')), flush=True)
    subprocess.run(['open', '-R', str(archive)], check=False)

def start():
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise RuntimeError('Start dit pakket op een Apple Silicon Mac, niet via Rosetta.')
    if is_running():
        print('Deze VM draait al. Gebruik Status.command.', flush=True)
        return
    if shutil.disk_usage(ROOT).free < 60 * 1024**3:
        raise RuntimeError('Houd minstens 60 GB vrij voor installatie en retourbestand.')
    for port in (SSH_PORT, VNC_PORT):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    for tool in ('qemu-system-aarch64', 'qemu-img', 'swtpm'):
        if not shutil.which(tool):
            raise RuntimeError('Ontbrekend programma: ' + tool)
    accelerators = run(['qemu-system-aarch64', '-accel', 'help'], capture_output=True, text=True).stdout
    if 'hvf' not in accelerators.split():
        raise RuntimeError('Deze QEMU heeft geen Apple hardwareversnelling (HVF).')
    manifest = json.loads((ROOT / 'manifest.json').read_text())
    if not (ROOT / '.verified').exists():
        print('Overdracht controleren (eenmalig)...', flush=True)
        for relative, expected in manifest['files'].items():
            path = (ROOT / relative).resolve()
            if not path.is_relative_to(ROOT) or digest(path) != expected:
                raise RuntimeError('Bestand beschadigd of gewijzigd: ' + relative)
        (ROOT / '.verified').write_text('verified\n')
    for name in ('windows-arm64.qcow2', 'windows-arm64-runners.qcow2'):
        info = json.loads(run(['qemu-img', 'info', '--output=json', VM / name], capture_output=True, text=True).stdout)
        if info.get('backing-filename'):
            raise RuntimeError('De schijf is niet zelfstandig: ' + name)
    RUNTIME.mkdir(mode=0o700, exist_ok=True)
    LOG.mkdir(exist_ok=True)
    for name in ('qmp.sock', 'tpm.sock'):
        path = RUNTIME / name
        if path.exists():
            path.unlink()
    run(['swtpm', 'socket', '--tpm2', '--tpmstate', 'dir=' + str(VM / 'tpm'),
         '--ctrl', 'type=unixio,path=' + str(RUNTIME / 'tpm.sock'),
         '--pid', 'file=' + str(RUNTIME / 'swtpm.pid'), '--daemon'])
    command = ['qemu-system-aarch64', '-name', 'nomercy-windows-arm64-portable',
               '-machine', 'virt-8.2,gic-version=3', '-accel', 'hvf', '-cpu', 'host', '-smp', '8', '-m', '8G',
               '-rtc', 'base=localtime,clock=host',
               '-drive', 'if=pflash,format=raw,unit=0,readonly=on,file=' + str(VM / 'AAVMF_CODE.fd'),
               '-drive', 'if=pflash,format=raw,unit=1,file=' + str(VM / 'AAVMF_VARS.fd'),
               '-device', 'ramfb', '-device', 'qemu-xhci', '-device', 'usb-kbd', '-device', 'usb-tablet',
               '-drive', 'if=none,id=system,format=qcow2,file=' + str(VM / 'windows-arm64.qcow2'),
               '-device', 'nvme,drive=system,serial=RNRWINARM64,bootindex=1',
               '-drive', 'if=none,id=runnerdata,format=qcow2,file=' + str(VM / 'windows-arm64-runners.qcow2'),
               '-netdev', f'user,id=net0,hostfwd=tcp:127.0.0.1:{SSH_PORT}-:22',
               '-device', 'virtio-net-pci,netdev=net0',
               '-device', 'nvme,drive=runnerdata,serial=RNRARM64DATA',
               '-chardev', 'socket,id=chrtpm,path=' + str(RUNTIME / 'tpm.sock'),
               '-tpmdev', 'emulator,id=tpm0,chardev=chrtpm', '-device', 'tpm-tis-device,tpmdev=tpm0',
               '-display', 'none', '-vnc', '127.0.0.1:8',
               '-qmp', 'unix:' + str(QMP) + ',server=on,wait=off',
               '-pidfile', str(RUNTIME / 'qemu.pid')]
    environment = dict(os.environ, TZ='Europe/Amsterdam')
    with (LOG / 'qemu.log').open('ab') as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                                   start_new_session=True, env=environment)
    print('Windows ARM64 start met HVF, 8 cores en 8 GB. Geen Windows-aanmelding nodig.', flush=True)
    print('Laat dit venster open. Status verschijnt alleen als de fase verandert.', flush=True)
    last = None
    started = time.monotonic()
    while process.poll() is None:
        try:
            status = guest_status()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            status = {'phase': 'booting', 'detail': 'Windows start of herstart.'}
        signature = (status.get('phase'), status.get('detail'))
        if signature != last:
            print(time.strftime('%H:%M:%S'), *signature, flush=True)
            last = signature
        if status.get('phase') in ('ready', 'error'):
            collect_logs()
            if status['phase'] == 'error':
                raise RuntimeError('Voorbereiding gestopt. Logs zijn bewaard; de VM blijft aan voor diagnose.')
            print('Beide service-account bouwtests geslaagd. Retourbestand wordt gemaakt.', flush=True)
            export_return()
            return
        if status.get('phase') == 'booting' and time.monotonic() - started > 1800:
            raise RuntimeError('Nog geen SSH na 30 minuten. VM blijft aan; gebruik Scherm.command en stuur logs terug.')
        time.sleep(60)
    raise RuntimeError('QEMU is gestopt; zie logs/qemu.log. Er wordt geen TCG-fallback gebruikt.')

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['start', 'status', 'stop', 'return'])
    action = parser.parse_args().action
    if action == 'start':
        start()
    elif action == 'status':
        print(json.dumps(guest_status(), indent=2))
    elif action == 'stop':
        stop()
    else:
        export_return()

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('\nFOUT:', error, file=sys.stderr, flush=True)
        sys.exit(1)
