"""Record and apply the operator-selected runner profile without changing identities.

Run in the controller. Runtime changes and migration must precede committing
their observed limits; this helper never stops or recreates a runner.
"""
import argparse
import ctypes
import json
import os
import re
from pathlib import Path
import select
import struct
import time

import providers
from control import agent_runtime
from control.forges import LiveForges
from control.secrets import SecretStore
from control.service import RunnerService
from store import schema

GIB = 1024 ** 3
RAM, DISK, CACHE = 16 * GIB, 100 * GIB, 40 * GIB
CORES = 8
LINUX_COMBINED = 32 * GIB
BASELINE = Path('/data/resource-uniformity-20261009-baseline.json')


class DatabaseChanges:
    """Wait on native Linux writes/renames, including SQLite's WAL sidecar."""
    def __init__(self, path):
        self.path = Path(path)
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        self.fd = self.libc.inotify_init1(os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), 'inotify_init1 failed')
        if self.libc.inotify_add_watch(self.fd, os.fsencode(self.path.parent),
                                      0x0002 | 0x0008 | 0x0080 | 0x0100 | 0x0200) < 0:
            os.close(self.fd)
            raise OSError(ctypes.get_errno(), 'inotify_add_watch failed')

    def wait(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                raise TimeoutError('Runner did not reach the required observed state')
            events = os.read(self.fd, 65536)
            offset = 0
            changed = False
            while offset < len(events):
                _, mask, _, length = struct.unpack_from('iIII', events, offset)
                name = os.fsdecode(events[offset + 16:offset + 16 + length].split(b'\0', 1)[0])
                offset += 16 + length
                if mask & 0x8000:
                    raise RuntimeError('Database watch was removed; refusing to poll')
                changed |= bool(mask & 0x4000) or name in {
                    self.path.name, self.path.name + '-wal', self.path.name + '-journal'}
            if changed:
                return

    def close(self):
        os.close(self.fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('status', 'commit-linux-memory', 'commit-linux-cache',
                                         'drain', 'resume', 'wait-drained', 'wait-online', 'wait-idle',
                                         'wait-worker-ready', 'verify-identities'))
    parser.add_argument('--runner')
    parser.add_argument('--generation', default='20261009')
    parser.add_argument('--host')
    parser.add_argument('--after')
    parser.add_argument('--minimum-memory-gib', type=int, default=0)
    parser.add_argument('--timeout-seconds', type=int, default=1800)
    args = parser.parse_args()
    assert re.fullmatch(r'[A-Za-z0-9-]{1,80}', args.generation)
    assert 1 <= args.timeout_seconds <= 432000
    env = SecretStore(schema.DB_PATH).overlay(os.environ)
    service = RunnerService(schema.DB_PATH, runtimes=agent_runtime.TABLE, env=env)
    baseline = json.loads(BASELINE.read_text())
    if args.mode == 'wait-worker-ready':
        assert args.host in {s['host_id'] for s in baseline['specs']}
        assert args.after and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z', args.after)
        changes = DatabaseChanges(schema.DB_PATH)
        deadline = time.monotonic() + args.timeout_seconds
        try:
            while True:
                worker = service.inventory.get(args.host)
                caps = worker.get('capabilities') or {}
                hardware = caps.get('hardware') or {}
                if (service.inventory.health(args.host) == 'healthy'
                        and worker.get('last_seen_at', '') > args.after
                        and hardware.get('memory_bytes', 0) >= args.minimum_memory_gib * GIB):
                    print(json.dumps({'worker': args.host, 'ready': True,
                                      'memory_bytes': hardware['memory_bytes']}), flush=True)
                    return
                changes.wait(deadline)
        finally:
            changes.close()
    if args.mode in ('commit-linux-cache', 'drain', 'resume', 'wait-drained', 'wait-online', 'wait-idle'):
        assert args.runner in {s['runner_id'] for s in baseline['specs']}
        spec = service.specs.get(args.runner)
        assert not spec.get('current_operation') or args.mode.startswith('wait-')
        if args.mode == 'commit-linux-cache':
            assert spec['platform'] == 'linux' and spec['actual_state'] == 'drained'
            policy = {'max_bytes': CACHE, 'enabled': True, 'on_clear': 'skip-if-busy'}
            service.fleets.set_defaults(spec['fleet_id'], {'cache_policy': policy})
            service.specs.update(args.runner, spec['spec_version'], cache_policy=policy)
        elif args.mode in ('drain', 'resume'):
            if args.mode == 'drain':
                provider = providers.by_key(spec['provider'])
                state = provider.job_state(spec, LiveForges(env).records(provider))
                assert state == providers.IDLE, 'Fresh forge state must be idle before maintenance'
            verb = 'drain' if args.mode == 'drain' else 'cancel_drain'
            op = service.act(args.runner, verb, requested_by='FiLL-authorized-uniform-resources',
                             idempotency_key='uniform-resources-%s-%s-%s' %
                             (args.generation, verb, args.runner))
            print(json.dumps({'runner_id': args.runner, 'operation': op, 'verb': verb}), flush=True)
        else:
            deadline = time.monotonic() + args.timeout_seconds
            # Install before the first read: no transition can be missed
            # between checking the state and blocking on the next event.
            changes = DatabaseChanges(schema.DB_PATH)
            try:
                while True:
                    spec = service.specs.get(args.runner)
                    if spec['actual_state'] == 'failed':
                        raise RuntimeError('Runner operation failed; inspect the operation before continuing')
                    target = ('drained',) if args.mode == 'wait-drained' else (
                        ('idle',) if args.mode == 'wait-idle' else ('idle', 'busy'))
                    ready = spec['actual_state'] in target
                    if ready and not spec['current_operation']:
                        if args.mode in ('wait-online', 'wait-idle'):
                            provider = providers.by_key(spec['provider'])
                            state = provider.job_state(spec, LiveForges(env).records(provider))
                            ready = state == providers.IDLE if args.mode == 'wait-idle' else state in (providers.IDLE, providers.BUSY)
                        if ready:
                            print(json.dumps({'runner_id': args.runner, 'state': spec['actual_state']}), flush=True)
                            return
                    changes.wait(deadline)
            finally:
                changes.close()
        return
    if args.mode == 'commit-linux-memory':
        cpus = json.loads(Path('/data/linux-uniform-profile.json').read_text())
        assert set(cpus) == {spec['runner_id'] for spec in service.specs.list() if spec['platform'] == 'linux'}
        for fleet in service.fleets.list():
            if fleet['platform'] == 'linux':
                service.fleets.set_defaults(fleet['fleet_id'], {
                    'cpu_limit': CORES,
                    'memory_limit': RAM, 'memory_swap_limit': LINUX_COMBINED,
                    'disk_limit': DISK})
        for spec in service.specs.list():
            if spec['platform'] == 'linux':
                service.specs.update(spec['runner_id'], spec['spec_version'],
                                     memory_limit=RAM, memory_swap_limit=LINUX_COMBINED,
                                     cpu_limit=cpus[spec['runner_id']], cpu_override=None,
                                     memory_override=None, disk_limit=DISK)
    if args.mode == 'verify-identities':
        current = {s['runner_id']: s for s in service.specs.list()}
        assert set(current) == {s['runner_id'] for s in baseline['specs']}
        for original in baseline['specs']:
            for key in ('fleet_id', 'host_id', 'registration_id', 'registration_uuid',
                        'provider', 'platform', 'architecture', 'display_name', 'labels'):
                assert current[original['runner_id']].get(key) == original.get(key), (original['runner_id'], key)
        print('All 19 runner identities and registrations preserved.')
    else:
        for spec in service.specs.list():
            print(json.dumps({k: spec.get(k) for k in
                              ('runner_id', 'display_name', 'platform', 'actual_state',
                               'memory_limit', 'disk_limit', 'cache_policy')}))


if __name__ == '__main__':
    main()
