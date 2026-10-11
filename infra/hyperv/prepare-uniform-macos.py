"""Stage independent macOS appliances while the existing runners stay online."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, '/opt/uniform-resources')
from agent.runtimes.macos_pool import MacAppliancePoolRuntime

GIB = 1024 ** 3
IDS = ('c7343225-7446-4b87-87a6-cb516522e1dc', 'fc5bc5c1-65da-4295-9109-58183d9c4d1f')
TEMPLATES = ('actions-runner-v2.338.0-macos-r20261008', 'forgejo-runner-v13.1.0-macos-r20260921')


def build():
    config = json.loads(Path('/etc/runner-agent/agent.json').read_text())
    config['appliance_pool'] = dict(
        image='sha256:ab06f62364694bc5c49b41ea9644d42da03f7af2b36d86acec008a725ae6e745',
        base_disk='/var/lib/runner-appliances/base/macos-sequoia-15.7.7-runner-debloated-20260924.qcow2',
        base_system='/var/lib/runner-appliances/bases/BaseSystem.img',
        nvram_seed='/var/lib/runner-appliances/base/OVMF_VARS-sequoia-debloated-20260924.fd',
        data_root='/var/lib/runner-appliances/instances', templates=list(TEMPLATES),
        base_guests_disabled=True, ssh_port_base=51100, runner_storage_limit=100 * GIB)
    config['capacity'] = {'max_runners': 2, 'memory_bytes': 36 * GIB}
    guest = dict(config['guest'])
    guest['password'] = Path(guest['password_file']).read_text().strip()
    return config, MacAppliancePoolRuntime(**config['appliance_pool'], guest=guest, tools=config['tools'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'upgrade-preparation-profile', 'boot', 'stage-guest', 'shutdown', 'release-preparation-limits'))
    parser.add_argument('--index', type=int, choices=(0, 1), default=0)
    args = parser.parse_args()
    config, pool = build()
    if args.mode == 'upgrade-preparation-profile':
        production = json.loads(subprocess.check_output(['docker', 'inspect', 'macos-sequoia']))[0]
        assert production['State']['Running']
        for rid, template in zip(IDS, TEMPLATES):
            record = pool._read(rid)
            assert record['initialized'] is False and record['listener_disabled'] is True
            assert pool._power(rid) == 'stopped'
            if (record['memory'], record['cpus']) == (16 * GIB, 8):
                continue
            directory = pool._directory(rid)
            before = directory / 'before-profile-16g-8cores-20261010.json'
            with before.open('x') as stream:
                json.dump(record, stream)
            before.chmod(0o600)
            # Retain the stopped preparation as well as its disk/NVRAM.
            # The live macOS appliance and its registrations are untouched.
            pool._write(record, retained=True)
            try:
                pool._command(['docker', 'rename', 'rnr-' + rid,
                               'uniform-preparation-before-' + rid])
            except BaseException:
                pool._write(record, retained=False)
                raise
            pool.prepare(rid, {'image': template, 'cpus': '8', 'memory': 16 * GIB,
                               'disk_limit': 100 * GIB})
            assert pool._power(rid) == 'stopped'
            print(json.dumps({'runner': rid, 'staged_memory_gib': 16,
                              'staged_cores': 8, 'powered_off': True}), flush=True)
        after = json.loads(subprocess.check_output(['docker', 'inspect', 'macos-sequoia']))[0]
        assert (after['Id'], after['State']['StartedAt'], after['State']['Running']) == (
            production['Id'], production['State']['StartedAt'], True)
        path = Path('/etc/runner-agent/agent.uniform-pool.json')
        path.write_text(json.dumps(config, indent=2)); path.chmod(0o600)
    elif args.mode == 'prepare':
        for rid, template in zip(IDS, TEMPLATES):
            pool.prepare(rid, {'image': template, 'cpus': '8', 'memory': 16 * GIB,
                               'disk_limit': 100 * GIB})
            assert pool._power(rid) == 'stopped'
            print(json.dumps({'runner': rid, 'staged': True, 'powered_off': True}), flush=True)
        p = Path('/etc/runner-agent/agent.uniform-pool.json')
        p.write_text(json.dumps(config, indent=2)); p.chmod(0o600)
    else:
        record = pool._read(IDS[args.index])
        if args.mode == 'shutdown':
            pool._shutdown(record)
        elif args.mode == 'release-preparation-limits':
            assert not record.get('initialized') and record.get('listener_disabled')
            assert pool._power(record['runner_id']) == 'stopped'
            pool._command(['docker', 'update', '--cpuset-cpus', '', '--cpu-shares', '1024',
                           'rnr-' + record['runner_id']], timeout=30)
        else:
            # Preparation shares a host with the existing production guest.
            # Give preparation a low scheduling weight, without pinning six
            # guest CPUs to two host CPUs. The first restricted boot had slow
            # disk flushes followed by a kernel panic; avoid CPU starvation
            # when testing whether a fresh boot can complete.
            # Restore the normal scheduling weight before cutover.
            assert not record.get('initialized') and record.get('listener_disabled')
            pool._command(['docker', 'update', '--cpuset-cpus', '', '--cpu-shares', '64',
                           'rnr-' + record['runner_id']], timeout=30)
            inner, _ = pool._boot(record, maintenance=True)
            inner._ensure_storage(record['runner_id'])
            if args.mode == 'stage-guest':
                source = Path('/var/lib/runner-appliances/uniform-templates.tgz')
                assert source.is_file(), 'Verified current runner templates must be staged first'
                guest, _, _ = pool._context(record)
                subprocess.run(['sshpass', '-f', config['guest']['password_file'], 'scp', '-O',
                                '-P', str(record['port']), '-o', 'StrictHostKeyChecking=no',
                                '-o', 'UserKnownHostsFile=/dev/null', str(source),
                                config['guest']['user'] + '@127.0.0.1:/tmp/uniform-templates.tgz'],
                               check=True, timeout=300)
                inner._fs.makedirs(config['tools']['templates'])
                ok, out, err = guest(['/usr/bin/tar', '-xzf', '/tmp/uniform-templates.tgz',
                                       '-C', config['tools']['templates']], timeout=300)
                assert ok, err
                assert set(TEMPLATES).issubset(inner._templates()), 'Current templates missing in guest'
                inner._fs.remove('/tmp/uniform-templates.tgz')
            print(json.dumps({'runner': record['runner_id'], 'quota': inner._owned_storage(record['runner_id'])}), flush=True)


if __name__ == '__main__':
    main()
