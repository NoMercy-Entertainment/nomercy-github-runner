"""Independent consoles preserve the serving guest through failed cutovers."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def console(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[2] / 'infra/hyperv/macos-uniform-consoles.py'
    spec = importlib.util.spec_from_file_location('macos_consoles', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.ROOT = tmp_path / 'checkpoints'
    module.CONFIG = tmp_path / 'agent.json'
    module.CONFIG.write_text(json.dumps({'runtime': 'macos-appliance', 'appliance_pool': {'max_instances': 2}}))
    original = dict(Id='original-id', Image='sha256:pinned-console',
                    Config={'Cmd': ['--addr', ':8080', '--host', 'macos-sequoia', '--port', '5999', '--no-url-password', '--basic-ui']},
                    HostConfig={'NetworkMode': module.NETWORK, 'RestartPolicy': {'Name': 'unless-stopped'}},
                    State={'Running': True})
    rows = {'novnc': original}
    for guest, _, _ in module.TARGETS:
        rows[guest] = dict(Id=guest, Config={'Labels': {'nomercy.runner_id': guest[4:]}},
                           NetworkSettings={'Networks': {}}, State={'Running': False})
    calls = []
    fail = set()

    def docker(*args):
        calls.append(args)
        if args[:3] == ('ps', '-a', '--format'):
            return '\n'.join(rows)
        if args[:2] == ('inspect', '--type'):
            return json.dumps([rows[args[-1]]])
        if args[:2] == ('network', 'connect'):
            rows[args[-1]]['NetworkSettings']['Networks'][module.NETWORK] = {'IPAddress': '172.18.0.3'}
        elif args[0] == 'create':
            name = args[args.index('--name') + 1]
            published = args[args.index('--publish') + 1]
            rows[name] = dict(Id=name, Image='sha256:pinned-console',
                              Config={'Cmd': list(args[-8:]), 'Labels': {'nomercy.macos-console': '20261011'}},
                              HostConfig={'NetworkMode': module.NETWORK,
                                          'PortBindings': {'8080/tcp': [{'HostIp': '0.0.0.0', 'HostPort': published.split(':')[1]}]}},
                              State={'Running': False})
        elif args[0] in ('start', 'stop'):
            if args[-1] in fail:
                raise RuntimeError('console could not bind')
            rows[args[-1]]['State']['Running'] = args[0] == 'start'
        else:
            raise AssertionError(args)
        return ''

    monkeypatch.setattr(module, 'docker', docker)
    return module, rows, calls, fail


def test_preparation_keeps_original_serving_and_guests_stopped(console):
    module, rows, calls, _ = console
    guest_states = {guest: copy.deepcopy(rows[guest]['State']) for guest, _, _ in module.TARGETS}
    module.prepare()
    module.prepare()
    assert rows['novnc']['State']['Running']
    assert all(rows[guest]['State'] == state for guest, state in guest_states.items())
    assert not any(call[0] in ('start', 'stop') for call in calls)
    assert len([call for call in calls if call[0] == 'create']) == 2


def test_foreign_guest_is_not_adopted(console):
    module, rows, calls, _ = console
    rows[module.TARGETS[0][0]]['Config']['Labels']['nomercy.runner_id'] = 'someone-else'
    with pytest.raises(AssertionError):
        module.prepare()
    assert rows['novnc']['State']['Running']
    assert not any(call[0] in ('create', 'start', 'stop', 'network') for call in calls)


def test_switch_requires_the_verified_pool_configuration(console):
    module, rows, calls, _ = console
    module.CONFIG.write_text(json.dumps({'runtime': 'macos-appliance'}))
    with pytest.raises(AssertionError):
        module.switch()
    assert rows['novnc']['State']['Running']
    assert not any(call[0] in ('start', 'stop') for call in calls)


def test_failed_second_console_restores_original_without_touching_guests(console):
    module, rows, calls, fail = console
    fail.add(module.TARGETS[1][1])
    with pytest.raises(RuntimeError, match='could not bind'):
        module.switch()
    assert rows['novnc']['State']['Running']
    assert rows['novnc']['Id'] == 'original-id'
    assert all(not rows[name]['State']['Running'] for guest, name, _ in module.TARGETS)
    assert all(not rows[guest]['State']['Running'] for guest, _, _ in module.TARGETS)
    assert not any(call[0] in ('start', 'stop') and call[-1].startswith('rnr-') for call in calls)


def test_successful_switch_and_rollback_preserve_original_identity(console):
    module, rows, _, _ = console
    module.switch()
    module.switch()
    assert not rows['novnc']['State']['Running']
    assert all(rows[name]['State']['Running'] for _, name, _ in module.TARGETS)
    module.rollback()
    module.rollback()
    assert rows['novnc']['State']['Running']
    assert rows['novnc']['Id'] == 'original-id'


def test_foreign_console_port_configuration_is_refused(console):
    module, rows, calls, _ = console
    module.prepare()
    rows[module.TARGETS[0][1]]['HostConfig']['PortBindings']['8080/tcp'][0]['HostPort'] = '1234'
    with pytest.raises(AssertionError):
        module.switch()
    assert rows['novnc']['State']['Running']
    assert not any(call[0] in ('start', 'stop') for call in calls)
