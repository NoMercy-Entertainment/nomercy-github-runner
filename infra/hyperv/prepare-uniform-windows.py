"""Prepare new protected limits; activate only with the runner service stopped.

Run elevated in the Windows worker. Registration, hooks and storage retain
their identity; no registrar is invoked and no service is started here.
"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import UUID

APP = Path(r'C:\ProgramData\nomercy\agent\app')
sys.path.insert(0, str(APP))
from agent.config import load
from agent.runtimes.windows_process import WindowsProcessRuntime

ROOT = Path(r'C:\ProgramData\nomercy\agent\uniform-20261010')
GIB = 1024 ** 3
OWNERS = {
    'rnr-windows-1': {'98ce7e34-1498-4825-acd5-4d58af67816f',
                      'fcbd05be-00e3-4974-b2c9-04aa4ef844e6'},
}


def registration(reg):
    files = [reg / '.runner', reg / 'agent' / '.runner']
    result = {str(p.relative_to(reg)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in files if p.is_file()}
    assert result, 'Registered identity must already exist'
    return result


def cpu_window(value):
    cores = set()
    for part in str(value or '0-7').split(','):
        if '-' in part:
            lo, hi = (int(v) for v in part.split('-'))
            cores.update(range(lo, hi + 1))
        else:
            cores.add(int(part))
    assert len(cores) >= 8
    return ','.join(str(v) for v in sorted(cores)[:8])


def physical_memory():
    class Memory(ctypes.Structure):
        _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
            (name, ctypes.c_uint64) for name in ('total', 'available', 'page_total',
                'page_available', 'virtual_total', 'virtual_available', 'extended')]
    memory = Memory()
    memory.length = ctypes.sizeof(memory)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(Memory)]
    kernel.GlobalMemoryStatusEx.restype = wintypes.BOOL
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(memory)):
        raise ctypes.WinError(ctypes.get_last_error())
    return memory.total


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('prepare', 'activate', 'rollback'))
    parser.add_argument('--runner', required=True)
    parser.add_argument('--drained', action='store_true',
                        help='Prepare an already drained service without starting it')
    parser.add_argument('--cpu-offset', type=int, choices=(0, 8),
                        help='Stage one of two disjoint eight-core windows before VM expansion')
    args = parser.parse_args()
    rid = str(UUID(args.runner))
    config = load(r'C:\ProgramData\nomercy\agent\agent.json')
    assert rid in OWNERS[config.host_id]
    runtime = WindowsProcessRuntime(tools=config.tools, storage=config.windows_storage)
    paths = runtime.paths(rid)
    reg = Path(paths['reg'])
    unit_path = reg / 'unit.json'
    saved_path = ROOT / (rid + '.json')
    if args.mode == 'prepare':
        running = runtime.status(rid).get('running')
        if args.drained:
            assert running is False and (reg / 'drain.request').is_file(), 'Drain must be locally confirmed'
        else:
            assert running is True
        original = json.loads(unit_path.read_text())
        assert original['runner_id'] == rid
        spec = {'image': (reg / '.template').read_text().strip(),
                'env': dict(original['env']), 'memory': str(16 * GIB),
                'cpus': '8', 'cpuset': cpu_window(original.get('cpuset')),
                'disk_limit': 100 * GIB}
        spec['env']['RUNNER_CACHE_MAX_BYTES'] = str(40 * GIB)
        if args.cpu_offset is not None:
            spec['cpuset'] = ','.join(str(v) for v in range(args.cpu_offset, args.cpu_offset+8))
        ROOT.mkdir(exist_ok=True)
        subprocess.run(['icacls', str(ROOT), '/inheritance:r', '/grant:r',
                        '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F', '/Q'],
                       check=True, capture_output=True)
        with saved_path.open('x', encoding='utf-8') as stream:
            json.dump({'host_id': config.host_id, 'original': original,
                       'registration': registration(reg), 'spec': spec}, stream)
        print(json.dumps({'runner_id': rid, 'configuration_ready': True,
                          'runner_restarted': False}), flush=True)
        return
    saved = json.loads(saved_path.read_text())
    assert saved['host_id'] == config.host_id
    assert runtime.status(rid).get('running') is False, 'Drain must complete first'
    assert registration(reg) == saved['registration']
    disk = runtime._storage.verify(rid) if runtime._storage else None
    assert disk and disk['virtual_bytes'] == 100 * GIB, 'Owned storage must already be migrated'
    spec = saved['spec']
    if args.mode == 'activate':
        assert physical_memory() >= 34 * GIB, 'Both 16-GiB runners need backed guest memory'
        assert os.cpu_count() > max(int(v) for v in spec['cpuset'].split(',')), 'Staged CPU window is unavailable'
    else:
        original = saved['original']
        spec = dict(spec, env=original['env'], memory=str(original['memory_bytes']),
                    cpus=str(original['cpus'] or ''), cpuset=original['cpuset'])
    runtime._acl(paths['root'], 'rnr-' + rid)
    if config.tools.get('short_workspaces'):
        from agent.windows_workspace import ensure_alias
        ensure_alias(config.tools['short_workspaces'], rid, paths['work'],
                     runtime._run, runtime._tools['icacls'], runtime._tools['powershell'])
    runtime._configure('rnr-' + rid, paths, spec)
    runtime._fs.write_text(str(unit_path), json.dumps(runtime._unit(rid, spec, paths)))
    runtime._lock_from_jobs('rnr-' + rid, paths, spec['image'].startswith('actions-runner'))
    assert registration(reg) == saved['registration']
    assert runtime.status(rid).get('running') is False
    print(json.dumps({'runner_id': rid, 'configuration_selected': args.mode,
                      'registration_preserved': True, 'awaiting_resume': True}), flush=True)


if __name__ == '__main__':
    main()
