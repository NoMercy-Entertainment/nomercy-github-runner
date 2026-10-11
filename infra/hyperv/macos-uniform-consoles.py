"""Stage independent consoles without starting or modifying either runner."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import urllib.request

ROOT = Path('/var/lib/runner-appliances/uniform-consoles-20261011')
CONFIG = Path('/etc/runner-agent/agent.json')
NETWORK = 'macos_runner_default'
TARGETS = (
    ('rnr-c7343225-7446-4b87-87a6-cb516522e1dc', 'macos-github-novnc', 8899),
    ('rnr-fc5bc5c1-65da-4295-9109-58183d9c4d1f', 'macos-forgejo-novnc', 8897),
)


def docker(*args):
    return subprocess.check_output(['docker', *args], text=True).strip()


def inspect(name):
    return json.loads(docker('inspect', '--type', 'container', name))[0]


def exists(name):
    return name in docker('ps', '-a', '--format', '{{.Names}}').splitlines()


def checkpoint(name, value):
    temporary = ROOT / (name + '.writing')
    fd = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    os.replace(temporary, ROOT / name)


def prepare():
    ROOT.mkdir(mode=0o700, exist_ok=True)
    os.chmod(ROOT, 0o700)
    path = ROOT / 'original.json'
    if not path.exists():
        original = inspect('novnc')
        assert original['Config']['Cmd'] == ['--addr', ':8080', '--host', 'macos-sequoia', '--port', '5999', '--no-url-password', '--basic-ui']
        assert original['HostConfig']['NetworkMode'] == NETWORK
        checkpoint('original.json', {'id': original['Id'], 'image': original['Image'],
                                    'restart': original['HostConfig']['RestartPolicy']['Name'],
                                    'running': original['State']['Running']})
    original = json.loads(path.read_text())
    for guest, console, port in TARGETS:
        data = inspect(guest)
        assert (data['Config'].get('Labels') or {}).get('nomercy.runner_id') == guest[4:]
        if NETWORK not in data['NetworkSettings']['Networks']:
            docker('network', 'connect', '--alias', guest, NETWORK, guest)
        command = ['--addr', ':8080', '--host', guest, '--port', '5999', '--no-url-password', '--basic-ui']
        if not exists(console):
            docker('create', '--name', console, '--network', NETWORK, '--restart', 'unless-stopped',
                   '--memory', '128m', '--cpus', '0.25', '--label', 'nomercy.macos-console=20261011',
                   '--publish', f'0.0.0.0:{port}:8080', original['image'], *command)
        data = inspect(console)
        assert data['Image'] == original['image'] and data['Config']['Cmd'] == command
        assert (data['Config'].get('Labels') or {}).get('nomercy.macos-console') == '20261011'
        assert data['HostConfig']['NetworkMode'] == NETWORK
        assert data['HostConfig']['PortBindings']['8080/tcp'] == [{'HostIp': '0.0.0.0', 'HostPort': str(port)}]
    checkpoint('prepared.json', {'guests': 2, 'original_console_retained': True})
    print('Two consoles staged; original console and runner processes left unchanged.', flush=True)


def switch():
    prepare()
    config = json.loads(CONFIG.read_text())
    assert config.get('runtime') == 'macos-appliance' and config.get('appliance_pool')
    original = json.loads((ROOT / 'original.json').read_text())
    data = inspect('novnc')
    assert data['Id'] == original['id']
    if data['State']['Running']:
        docker('stop', '--time', '15', 'novnc')
    try:
        for _, console, _ in TARGETS:
            if not inspect(console)['State']['Running']:
                docker('start', console)
    except BaseException:
        rollback()
        raise
    checkpoint('switched.json', {'guests': 2, 'original_id': original['id']})


def rollback():
    original = json.loads((ROOT / 'original.json').read_text())
    assert inspect('novnc')['Id'] == original['id']
    for _, console, _ in TARGETS:
        if exists(console) and inspect(console)['State']['Running']:
            docker('stop', '--time', '15', console)
    if original['running'] and not inspect('novnc')['State']['Running']:
        docker('start', 'novnc')


def verify():
    for guest, console, port in TARGETS:
        assert inspect(console)['State']['Running'] is True
        address = inspect(guest)['NetworkSettings']['Networks'][NETWORK]['IPAddress']
        with socket.create_connection((address, 5999), timeout=15) as connection:
            assert connection.recv(12).startswith(b'RFB '), 'QEMU console did not provide a VNC greeting'
        with urllib.request.urlopen(f'http://127.0.0.1:{port}/', timeout=15) as response:
            assert response.status == 200
    checkpoint('verified.json', {'independent_qemu_consoles': 2, 'http_ports': [8899, 8897]})
    print('Both independent QEMU consoles verified.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'switch', 'rollback', 'verify'))
    args = parser.parse_args()
    globals()[args.mode]()
