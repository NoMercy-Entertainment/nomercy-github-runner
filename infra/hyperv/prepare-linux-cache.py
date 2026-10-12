"""Stage a stopped replacement with all existing runner volumes and identity.

The original stays running during preparation. Activation is permitted only
after the controller has drained it; no volume or registration is removed.
Run as root on the owning Linux worker, after deploying canonical unit filtering.
"""
import argparse
from copy import deepcopy
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
from urllib.parse import quote
from uuid import UUID

ROOT = Path('/var/lib/uniform-linux-cache-20261010')
CACHE = 40 * 1024 ** 3


class Engine(http.client.HTTPConnection):
    def __init__(self):
        super().__init__('localhost', timeout=1500)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect('/var/run/docker.sock')


def api(method, path, body=None):
    connection = Engine()
    try:
        encoded = json.dumps(body).encode() if body is not None else None
        connection.request(method, path, encoded,
                           {'Content-Type': 'application/json'} if encoded else {})
        response = connection.getresponse()
        result = response.read()
        if response.status not in (200, 201, 204):
            message = json.loads(result).get('message', '') if result else ''
            raise RuntimeError('Docker request failed: %s %s (%s): %s' %
                               (method, path, response.status, message[:500]))
        return json.loads(result) if result else None
    finally:
        connection.close()


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=1500).strip()


def inspect(name):
    return json.loads(docker('inspect', name))[0]


def private(path, data):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump(data, stream)
        stream.flush()
        os.fsync(stream.fileno())


def mounts(unit):
    return sorted((mount['Destination'], mount['Type'], mount['Source'], mount['RW'])
                  for mount in unit['Mounts'])


def digest_registration(unit):
    mount = next(mount for mount in unit['Mounts']
                 if mount['Destination'] == '/runner/reg')
    volume = json.loads(docker('volume', 'inspect', mount['Name']))[0]
    rid = unit['Config']['Labels']['nomercy.runner_id']
    assert volume['Labels']['nomercy.runner_id'] == rid
    assert volume['Labels']['nomercy.area'] == 'reg'
    options = volume.get('Options') or {}
    assert options.get('type') == 'none' and options.get('o') == 'bind'
    root = Path(options['device'])
    assert root == Path('/var/lib/runner-data/runners') / rid / 'mnt/reg'
    assert not root.is_symlink()
    files = [path for path in root.rglob('.runner') if path.is_file()]
    assert files, 'Existing registration not found in the owned registration volume'
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in files}


def changed_paths(unit):
    # Runtime sockets are recreated at boot and are not persistent job data.
    return sorted(line for line in docker('diff', unit['Id']).splitlines()
                  if line[2:] != '/run' and not line[2:].startswith('/run/'))


def prepare(rid):
    original = inspect('rnr-' + rid)
    assert original['Config']['Labels']['nomercy.runner_id'] == rid
    assert original['State']['Running'] and not original['State']['Restarting']
    assert original['HostConfig']['Memory'] == 16 * 1024 ** 3
    assert original['HostConfig']['NanoCpus'] == 8 * 1000 ** 3
    assert original['HostConfig']['NetworkMode'] in ('bridge', 'default')
    assert not original['HostConfig'].get('AutoRemove')
    assert not original['HostConfig'].get('ContainerIDFile')
    assert all(mount['Type'] in ('volume', 'tmpfs') for mount in original['Mounts'])
    registered = digest_registration(original)
    directory = ROOT / rid
    if directory.exists():
        assert not directory.is_symlink()
        assert not (directory / 'staged.json').exists(), 'Replacement already prepared'
        saved = json.loads((directory / 'original.json').read_text())
        assert saved['Id'] == original['Id']
        assert saved['State']['StartedAt'] == original['State']['StartedAt']
        assert registered == json.loads((directory / 'registration.json').read_text())
        assert changed_paths(original) == json.loads((directory / 'root-changes.json').read_text())
    else:
        directory.mkdir(mode=0o700)
        private(directory / 'original.json', original)
        private(directory / 'registration.json', registered)
        private(directory / 'root-changes.json', changed_paths(original))
    # Existing units contain earlier verified /runner/run patches. Preserve
    # their writable layer as well as all mounted storage; never fall back
    # to an older image and lose those patches. Commit does not pause jobs.
    version = api('GET', '/version')['ApiVersion']
    image = api('POST', '/v%s/commit?container=%s&pause=false&repo=%s&tag=%s' %
                (version, original['Id'], quote('nomercy/uniform-preserved', safe=''),
                 rid + '-20261010'))['Id']
    config = deepcopy(original['Config'])
    for key in ('OnBuild', 'ArgsEscaped'):
        config.pop(key, None)
    config['Image'] = image
    config['Env'] = [entry for entry in config.get('Env', [])
                     if not entry.startswith('RUNNER_BUILD_CACHE_GC=')]
    config['Env'].append('RUNNER_BUILD_CACHE_GC=%dB' % CACHE)
    host = deepcopy(original['HostConfig'])
    host['RestartPolicy'] = {'Name': 'no', 'MaximumRetryCount': 0}
    # Include anonymous image volumes too, so the replacement cannot silently
    # receive an empty /data or toolchain volume.
    host['Binds'] = [mount['Name'] + ':' + mount['Destination'] +
                     (':rw' if mount['RW'] else ':ro')
                     for mount in original['Mounts'] if mount['Type'] == 'volume']
    host.pop('Mounts', None)
    config['HostConfig'] = host
    staged_id = api('POST', '/v%s/containers/create?name=%s' % (
        version, quote('uniform-stage-' + rid)), config)['Id']
    staged = inspect(staged_id)
    assert staged['State']['Status'] == 'created' and not staged['State']['Running']
    assert mounts(staged) == mounts(original)
    assert staged['HostConfig']['Memory'] == original['HostConfig']['Memory']
    assert staged['HostConfig']['MemorySwap'] == original['HostConfig']['MemorySwap']
    assert staged['HostConfig']['CpusetCpus'] == original['HostConfig']['CpusetCpus']
    assert staged['HostConfig']['NanoCpus'] == original['HostConfig']['NanoCpus']
    assert staged['Config']['Labels'] == original['Config']['Labels']
    after = inspect(original['Id'])
    assert after['State']['StartedAt'] == original['State']['StartedAt']
    assert after['State']['Running']
    private(directory / 'staged.json', {'source_id': original['Id'], 'staged_id': staged_id,
                                       'image': image})
    print(json.dumps({'runner_id': rid, 'replacement_ready': True,
                      'source_restarted': False}), flush=True)


def activate(rid):
    directory = ROOT / rid
    saved = json.loads((directory / 'staged.json').read_text())
    original = inspect('rnr-' + rid)
    assert original['Id'] == saved['source_id']
    assert original['State']['Status'] == 'exited' and not original['State']['Running']
    # GitHub's fenced-group drain uses the idle-only stop path. Disable its
    # former restart policy before retaining the stopped rollback unit.
    if original['HostConfig']['RestartPolicy']['Name'] != 'no':
        docker('update', '--restart=no', original['Id'])
        original = inspect(original['Id'])
    assert original['HostConfig']['RestartPolicy']['Name'] == 'no'
    assert changed_paths(original) == json.loads((directory / 'root-changes.json').read_text()), \
        'Source root changed after staging; preserve and inspect it before activation'
    expected = json.loads((directory / 'registration.json').read_text())
    assert digest_registration(original) == expected
    staged = inspect(saved['staged_id'])
    assert staged['Name'] == '/uniform-stage-' + rid
    assert not staged['State']['Running'] and staged['State']['Status'] == 'created'
    assert mounts(staged) == mounts(original)
    # Renaming never alters mounted storage. Keep the stopped original for
    # rollback; the runtime's canonical-name check excludes that copy.
    docker('rename', original['Id'], 'uniform-rollback-' + rid)
    try:
        docker('rename', staged['Id'], 'rnr-' + rid)
    except BaseException:
        docker('rename', original['Id'], 'rnr-' + rid)
        raise
    assert inspect('rnr-' + rid)['Id'] == staged['Id']
    assert digest_registration(inspect('rnr-' + rid)) == expected
    print(json.dumps({'runner_id': rid, 'replacement_selected': True,
                      'registration_preserved': True, 'awaiting_resume': True}), flush=True)


def verify(rid):
    directory = ROOT / rid
    saved = json.loads((directory / 'staged.json').read_text())
    unit = inspect('rnr-' + rid)
    assert unit['Id'] == saved['staged_id'] and unit['State']['Running']
    assert not unit['State']['Restarting']
    assert unit['HostConfig']['Memory'] == 16 * 1024 ** 3
    assert unit['HostConfig']['NanoCpus'] == 8 * 1000 ** 3
    assert len(unit['HostConfig']['CpusetCpus'].split(',')) == 8
    assert digest_registration(unit) == json.loads((directory / 'registration.json').read_text())
    daemon = json.loads(docker('exec', unit['Id'], 'cat', '/runner/reg/daemon.json'))
    assert daemon['builder']['gc']['enabled'] is True
    assert daemon['builder']['gc']['policy'] == [{'maxUsedSpace': '%dB' % CACHE}]
    version = docker('exec', unit['Id'], 'docker', 'info', '--format', '{{.ServerVersion}}')
    assert version, 'Nested build engine must be healthy'
    print(json.dumps({'runner_id': rid, 'verified': True, 'cores': 8,
                      'memory_gib': 16, 'cache_gib': 40, 'registration_preserved': True}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'activate', 'verify'))
    parser.add_argument('--runner', required=True)
    args = parser.parse_args()
    rid = str(UUID(args.runner))
    ROOT.mkdir(mode=0o700, exist_ok=True)
    assert not ROOT.is_symlink()
    os.chmod(ROOT, 0o700)
    globals()[args.mode](rid)
