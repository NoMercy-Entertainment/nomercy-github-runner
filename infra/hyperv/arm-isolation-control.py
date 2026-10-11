"""Scoped controller operations for the Windows ARM64 host migration.

Run inside the existing controller with PYTHONPATH=/app. No re-enrolment,
capacity changes, replacement registrations or controller restart.
"""
import argparse
from contextlib import closing
import json
import os
import time
from pathlib import Path
import providers
from dbwatch import DatabaseChanges

from control import agent_runtime, audit, certificates
from control.agent_client import AgentClient
from control.forges import LiveForges
from control.secrets import SecretStore
from control.service import RunnerService
from store import schema

HOST = 'windows-arm64-1'
IDS = ('246e44e4-c7a7-4a67-ad61-6e5a09ab5a6b',
       '34746f24-145b-4d1c-9b97-24ea77db1e81')
OLD = 'https://10.77.0.40:8445'
NEW = 'https://10.77.0.50:8445'
BASELINE = Path('/data/arm-isolation-20261009-baseline-v2.json')
FIELDS = ('runner_id', 'fleet_id', 'host_id', 'registration_id',
          'registration_uuid', 'exec_unit_ref', 'provider', 'platform', 'architecture',
          'runtime_template', 'labels', 'runner_group', 'cpu_limit', 'memory_limit',
          'memory_swap_limit', 'disk_limit', 'cache_policy', 'cpu_override', 'memory_override')


def identity(spec):
    return {key: spec.get(key) for key in FIELDS}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('baseline', 'status', 'drain',
                                         'endpoint', 'rollback-endpoint',
                                         'resume', 'verify', 'wait-drained',
                                         'wait-healthy', 'wait-online', 'wait-forges'))
    mode = parser.parse_args().mode
    env = SecretStore(schema.DB_PATH).overlay(os.environ)
    service = RunnerService(schema.DB_PATH, runtimes=agent_runtime.TABLE, env=env)
    specs = [service.specs.get(rid) for rid in IDS]
    assert all(spec and spec['host_id'] == HOST for spec in specs)
    all_specs = service.specs.list()
    if mode.startswith('wait-'):
        deadline = time.monotonic() + 1800
        previous = None
        with closing(DatabaseChanges(schema.DB_PATH)) as changes:
            while True:
                specs = [service.specs.get(rid) for rid in IDS]
                health = service.inventory.health(HOST)
                forge_states = None
                if mode == 'wait-forges':
                    forges = LiveForges(env)
                    forge_states = tuple(providers.by_key(s['provider']).job_state(
                        s, forges.records(providers.by_key(s['provider']))) for s in specs)
                summary = (health, tuple((s['actual_state'], s['desired_state'], s['current_operation']) for s in specs), forge_states)
                if summary != previous:
                    print(json.dumps({'health': health, 'runners': summary[1], 'forge_states': forge_states}), flush=True)
                    previous = summary
                if mode == 'wait-drained':
                    ready = all(s['actual_state'] == 'drained' and s['desired_state'] == 'drained'
                                and not s['current_operation'] for s in specs)
                elif mode == 'wait-healthy':
                    ready = health == 'healthy' and service.inventory.get(HOST)['endpoint'] == NEW
                else:
                    ready = health == 'healthy' and all(s['actual_state'] in ('idle', 'busy')
                        and s['desired_state'] == 'running' and not s['current_operation'] for s in specs)
                    if mode == 'wait-forges':
                        ready = ready and all(state in (providers.IDLE, providers.BUSY) for state in forge_states)
                if ready:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Timed out: ' + mode)
                changes.wait(max(0, deadline - time.monotonic()))
    elif mode == 'baseline':
        assert not BASELINE.exists(), 'Baseline already recorded'
        assert all(s['actual_state'] == 'idle' and s['desired_state'] == 'running'
                   and not s['current_operation'] for s in specs)
        BASELINE.write_text(json.dumps({
            'runners': [identity(s) for s in all_specs],
            'worker': service.inventory.get(HOST),
        }, indent=2))
        print('Baseline saved; all runner identities recorded')
    elif mode in ('drain', 'resume'):
        for spec in specs:
            if mode == 'resume' and spec['actual_state'] in ('idle', 'busy') and spec['desired_state'] == 'running':
                continue
            verb = ('drain' if mode == 'drain' else
                    'cancel_drain' if spec['actual_state'] == 'drained' else 'start')
            operation = service.act(spec['runner_id'], verb,
                requested_by='FiLL-authorized-ARM-isolation',
                idempotency_key=f'arm-isolation-20261009-{verb}-{spec["runner_id"]}')
            print(json.dumps({'runner_id': spec['runner_id'], 'operation': operation, 'verb': verb}))
    elif mode in ('endpoint', 'rollback-endpoint'):
        previous, target = (OLD, NEW) if mode == 'endpoint' else (NEW, OLD)
        # A cleanly stopped, network-fenced source can be moved while its
        # existing drain is still pending after an internal Windows reboot.
        # The dedicated destination stays blocked from both forges until
        # that same drain finishes through the restored agent endpoint.
        assert all(s['actual_state'] in ('draining', 'drained') and s['desired_state'] == 'drained'
                   for s in specs), 'Both runners must have a drain requested'
        for spec in specs:
            if spec['current_operation']:
                assert service.operations.get(spec['current_operation'])['verb'] == 'drain'
        forges = LiveForges(env)
        for spec in specs:
            provider = providers.by_key(spec['provider'])
            assert provider.job_state(spec, forges.records(provider)) == providers.OFFLINE, 'Forge must confirm offline'
        with schema.connect(schema.DB_PATH) as conn:
            cursor = conn.execute('UPDATE workers SET endpoint=? WHERE host_id=? AND endpoint=?',
                                  (target, HOST, previous))
            assert cursor.rowcount == 1, 'Unexpected current endpoint'
        audit.record(schema.DB_PATH, verb='migrate-worker-endpoint', decision='accepted',
                     actor='FiLL-authorized-ARM-isolation',
                     parameters={'host_id': HOST, 'previous': previous, 'endpoint': target})
        print('Endpoint changed to ' + target + '; certificate identity preserved')
    elif mode == 'verify':
        baseline = json.loads(BASELINE.read_text())
        assert {s['runner_id']: identity(s) for s in all_specs} == {
            s['runner_id']: s for s in baseline['runners']}, 'Runner identity or limits changed'
        worker = service.inventory.get(HOST)
        assert worker['endpoint'] == NEW
        assert worker['certificate_fingerprint'] == baseline['worker']['certificate_fingerprint']
        assert service.inventory.health(HOST) == 'healthy'
        client = AgentClient(service.inventory,
            *certificates.resolve_paths(os.environ.get('CONTROL_TLS_DIR', '/data/control-tls'), 'controller'),
            timeout=115)
        hello = client.call(HOST, 'hello', timeout=115)
        assert hello, 'No authenticated agent reply'
        assert all(s['actual_state'] in ('idle', 'busy') and s['desired_state'] == 'running'
                   and not s['current_operation'] for s in specs)
        forges = LiveForges(env)
        records = {key: forges.records(providers.by_key(key))
                   for key in {s['provider'] for s in all_specs if s['actual_state'] != 'absent'}}
        online = []
        for spec in all_specs:
            if spec['actual_state'] == 'absent':
                continue
            provider = providers.by_key(spec['provider'])
            state = provider.job_state(spec, records[spec['provider']])
            assert state in (providers.IDLE, providers.BUSY), f'{spec["runner_id"]}: forge reports {state}'
            online.append({'runner_id': spec['runner_id'], 'fleet': spec['fleet_id'], 'forge_state': state})
        print(json.dumps({'fresh_forge_verification': online}, indent=2))
        print('Verified: same runner IDs, registrations, limits and certificate; new endpoint authenticates')
    print(json.dumps({'health': service.inventory.health(HOST),
        'endpoint': service.inventory.get(HOST)['endpoint'],
        'runners': [{k: s.get(k) for k in ('runner_id', 'fleet_id', 'actual_state',
                                          'desired_state', 'current_operation', 'last_error')}
                    for s in [service.specs.get(rid) for rid in IDS]]}, indent=2))


if __name__ == '__main__':
    main()
