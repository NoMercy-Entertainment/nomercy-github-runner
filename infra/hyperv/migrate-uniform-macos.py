"""Copy quiescent registered macOS runners into their prepared owned guests.

The original guest and its data stay intact. This helper never registers a
runner, alters a forge, switches the agent configuration, or stops a live job.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys

sys.path.insert(0, '/opt/uniform-resources')
sys.path.insert(0, '/tmp')
from agent.runtimes.guest_ssh import GuestExec, GuestFs
from agent.runtimes.macos_appliance import MacApplianceRuntime, ADOPTED_MARKER
from agent import naming
_module_spec = importlib.util.spec_from_file_location(
    'prepare_uniform_macos', Path(__file__).with_name('prepare-uniform-macos.py'))
_prepare = importlib.util.module_from_spec(_module_spec)
_module_spec.loader.exec_module(_prepare)
build, IDS, TEMPLATES, GIB = _prepare.build, _prepare.IDS, _prepare.TEMPLATES, _prepare.GIB

ROOT = Path('/var/lib/runner-appliances/uniform-migration-20261011')
IDENTITIES = Path('/tmp/macos-uniform-identities.json')


def private_json(path, value):
    temp = path.with_suffix('.writing')
    with temp.open('x') as stream:
        os.chmod(temp, 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def source():
    config = json.loads(Path('/etc/runner-agent/agent.json').read_text())
    assert not config.get('appliance_pool'), 'Source agent already switched to the pool'
    opts = config['guest']
    guest = GuestExec(opts['host'], opts['user'], opts.get('port', 50922),
                      password=Path(opts['password_file']).read_text().strip())
    return config, guest, MacApplianceRuntime(run=guest, fs=GuestFs(guest),
                                             tools=config['tools'], remote=True)


def stopped(inner):
    for rid in IDS:
        assert inner.status(rid).get('running') is False, 'Both original listeners must be stopped'


def registration(fs, home, provider):
    names = ('.runner', '.credentials', '.credentials_rsaparams') if provider == 'github' else ('.runner',)
    hashes = {}
    for name in names:
        path = home + '/' + name
        assert fs.exists(path), 'Registration file missing: ' + name
        hashes[name] = hashlib.sha256(fs.read_text(path).encode()).hexdigest()
    data = json.loads(fs.read_text(home + '/.runner'))
    return dict(hashes=hashes, id=str(data['agentId'] if provider == 'github' else data['id']),
                uuid=data.get('uuid'))


def scp(config, port, src, dst, upload=False):
    endpoint = config['guest']['user'] + '@127.0.0.1:'
    command = ['sshpass', '-f', config['guest']['password_file'], 'scp', '-O', '-P', str(port),
               '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null']
    command += [str(src), endpoint + str(dst)] if upload else [endpoint + str(src), str(dst)]
    subprocess.run(command, check=True, timeout=900, stdout=subprocess.DEVNULL)


def check_identity(proof, expected):
    assert proof['id'] == str(expected['registration_id']), 'Forge registration ID differs'
    if expected.get('registration_uuid'):
        assert proof['uuid'] == expected['registration_uuid'], 'Forge registration UUID differs'


def capture(index):
    config, guest, inner = source()
    stopped(inner)
    rid = IDS[index]
    expected = next(r for r in json.loads(IDENTITIES.read_text()) if r['runner_id'] == rid)
    paths = inner.paths(rid)
    adopted = inner.adopted(rid)
    definition = plistlib.loads(inner._fs.read_text((adopted or {}).get('plist') or paths['plist']).encode())
    legacy = definition['WorkingDirectory'] if adopted else None
    home = paths['reg'] + '/agent' if expected['provider'] == 'github' else legacy or paths['reg']
    proof = registration(inner._fs, home, expected['provider'])
    check_identity(proof, expected)
    directory = ROOT / naming.check(rid)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    checkpoint = directory / 'capture.json'
    assert not checkpoint.exists(), 'Capture already exists; inspect it before replacing data'
    remote = '/tmp/uniform-capture-' + rid + '.tgz'
    # The archive contains credentials: make it private before tar writes
    # any bytes, including while the capture is still in progress.
    command = 'umask 077; exec /usr/bin/tar -czf %s -C %s .' % (
        shlex.quote(remote), shlex.quote(paths['root']))
    ok, out, err = guest(['/bin/sh', '-c', command], timeout=900)
    assert ok, 'Owned data capture failed'
    inner._fs.chmod(remote, 0o600)
    scp(config, config['guest'].get('port', 50922), remote, directory / 'owned.tgz')
    os.chmod(directory / 'owned.tgz', 0o600)
    inner._fs.remove(remote)
    if legacy:
        # Only the existing registration is transplanted; the staged template
        # supplies the daemon and owned work/cache configuration.
        text = inner._fs.read_text(legacy + '/.runner')
        credential = directory / 'forgejo.runner'
        fd = os.open(credential, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as stream:
            stream.write(text)
    stopped(inner)
    assert registration(inner._fs, home, expected['provider']) == proof, 'Registration changed during capture'
    private_json(checkpoint, dict(runner_id=rid, expected=expected, registration=proof,
                                environment=definition.get('EnvironmentVariables', {}),
                                legacy_directory=legacy, captured_at=datetime.now(timezone.utc).isoformat()))
    print(json.dumps({'runner': rid, 'captured': True, 'registration_preserved': True}), flush=True)


def install(index):
    _, _, original = source()
    config, pool = build()
    rid = IDS[index]
    record = pool._read(rid)
    assert not record['initialized'] and record['listener_disabled'], 'Target already initialized'
    assert record['memory'] == 16 * GIB and record['cpus'] == 8
    assert pool._power(IDS[1-index]) == 'stopped', 'Prepare one target at a time'
    assert next(int(l.split()[1])*1024 for l in Path('/proc/meminfo').read_text().splitlines()
                if l.startswith('MemTotal:')) >= 38 * GIB, 'Approved host capacity is not active'
    directory = ROOT / rid
    captured = json.loads((directory / 'capture.json').read_text())
    assert captured['runner_id'] == rid
    # The production listener may resume after the short, quiescent capture.
    # The target remains disabled throughout installation and restart proof;
    # copying the same registration must never create a second live listener.
    source_paths = original.paths(rid)
    source_home = (source_paths['reg'] + '/agent' if captured['expected']['provider'] == 'github'
                   else captured['legacy_directory'] or source_paths['reg'])
    assert registration(original._fs, source_home, captured['expected']['provider']) == captured['registration']
    inner, _ = pool._boot(record, maintenance=True)
    inner._owned_storage(rid)
    guest, _, _ = pool._context(record)
    paths = inner.paths(rid)
    scp(config, record['port'], directory / 'owned.tgz', '/tmp/uniform-owned.tgz', upload=True)
    ok, out, err = guest(['/usr/bin/tar', '-xzf', '/tmp/uniform-owned.tgz', '-C', paths['root']], timeout=900)
    assert ok, 'Owned data restore failed'
    inner._fs.remove('/tmp/uniform-owned.tgz')
    marker = paths['root'] + '/' + ADOPTED_MARKER
    if inner._fs.exists(marker):
        # Keep the source adoption record without directing the new guest
        # back to the primary guest's launchd job and legacy paths.
        ok, _, _ = guest(['/bin/mv', marker, marker + '.before-uniform-migration'])
        assert ok
    if captured['expected']['provider'] == 'forgejo':
        inner._fs.makedirs(paths['reg'])
        inner._fs.write_text(paths['reg'] + '/.runner', (directory / 'forgejo.runner').read_text(), mode=0o600)
    home = paths['reg'] + '/agent' if captured['expected']['provider'] == 'github' else paths['reg']
    assert registration(inner._fs, home, captured['expected']['provider']) == captured['registration']
    spec = dict(image=TEMPLATES[index], cpus='8', memory=16*GIB, disk_limit=100*GIB,
                env=dict(captured['environment'], RUNNER_CACHE_MAX_BYTES=str(40*GIB)))
    # No registration or listener start: a target must not briefly claim a
    # new job merely to prepare its launchd definition.
    inner.create(rid, spec, start=False)
    pool._write(record, initialized=True)
    assert inner.status(rid).get('running') is False
    pool._write(record, listener_disabled=True)
    assert registration(inner._fs, home, captured['expected']['provider']) == captured['registration']
    pool._shutdown(record)
    inner, _ = pool._boot(record, maintenance=True)
    assert inner.status(rid).get('running') is False, 'Listener restarted during maintenance boot'
    assert registration(inner._fs, home, captured['expected']['provider']) == captured['registration']
    assert inner._owned_storage(rid)['CapacityQuota'] == 100*GIB
    ok, hardware, _ = pool._context(record)[0](['/usr/sbin/sysctl', 'hw.logicalcpu', 'hw.memsize'])
    assert ok, 'Cannot verify guest hardware'
    values = dict(line.split(': ', 1) for line in hardware.strip().splitlines())
    assert int(values['hw.logicalcpu']) == 8 and int(values['hw.memsize']) == 16*GIB
    pool._shutdown(record)
    assert registration(original._fs, source_home, captured['expected']['provider']) == captured['registration']
    private_json(directory / 'installed.json', dict(runner_id=rid, registration=captured['registration'],
                 memory_gib=16, cores=8, storage_gib=100, cache_gib=40, restart_proved=True,
                 installed_at=datetime.now(timezone.utc).isoformat()))
    print(json.dumps({'runner': rid, 'installed': True, 'registration_preserved': True,
                      'maintenance_restart_proved': True}), flush=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=('capture','install'))
    parser.add_argument('--index',type=int,choices=(0,1),required=True)
    args=parser.parse_args()
    ROOT.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(ROOT,0o700)
    (capture if args.mode=='capture' else install)(args.index)

if __name__=='__main__':
    main()
