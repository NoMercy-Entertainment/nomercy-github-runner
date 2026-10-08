import json
from pathlib import Path
import re
import subprocess

import pytest

from agent import windows_registration as registration
from agent import windows_timeouts
from agent.runtimes.windows_process import WindowsProcessRuntime, WindowsRegistrar

RID = '3f2504e0-4f89-41d3-9a0c-0305e82c3301'


@pytest.mark.parametrize('architecture', ['ARM64', 'aarch64'])
def test_emulated_arm_registration_keeps_outer_waits_longer_than_children(monkeypatch, tmp_path, architecture):
    monkeypatch.setattr(windows_timeouts.platform, 'machine', lambda: architecture)
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{}', '')

    registration.execute_registration('D:/owned', {'plan': {'token': 'secret'}}, {}, run=run)
    script_timeout = calls[-1][1]['timeout']
    assert script_timeout > 230, 'Observed ARM PowerShell startup already exceeds the former script bound'
    assert 'secret' not in ' '.join(calls[-1][0])

    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def send_bytes(self, data): pass
        def poll(self, timeout): return True
        def recv_bytes(self, maximum):
            return json.dumps({'ok': True, 'out': '{}', 'error': ''}).encode()

    pipe_timeouts = []
    def connect(*args, **kwargs):
        pipe_timeouts.append(kwargs['timeout'])
        return Connection()

    key = tmp_path / 'registration.key'
    key.write_bytes(b'k' * 32)
    monkeypatch.setattr(registration, 'key_path', lambda _: key)
    registration.request_registration(RID, {}, connect=connect)
    assert pipe_timeouts[-1] > script_timeout
    registration.request_registration(RID, {}, timeout=0.2, connect=connect)
    assert pipe_timeouts[-1] == 0.2, 'Explicit caller deadlines remain enforced'

    def runtime_run(args, **kwargs):
        calls.append((args, kwargs))
        return True, '{"registration_id":"test"}', ''

    WindowsProcessRuntime(run=runtime_run)._registration_key(RID, 'ensure')
    assert calls[-1][1]['timeout'] >= 300
    class Files:
        def exists(self, path): return True
    WindowsRegistrar(run=runtime_run, fs=Files()).register(RID, {})
    assert calls[-1][1]['timeout'] > windows_timeouts.registration_limits()['pipe']


def test_arm_template_children_fit_inside_handler_budget():
    root = Path(__file__).resolve().parents[2] / 'infra/windows/templates'
    for template in ('actions-runner-v2.338.0-windows-arm64', 'forgejo-runner-v13.1.0-windows-arm64'):
        source = (root / template / 'register.ps1').read_text(encoding='utf-8-sig')
        milliseconds = int(re.search(r'WaitForExit\((\d+)\)', source)[1])
        assert milliseconds / 1000 + 230 < windows_timeouts.registration_limits('arm64')['script']


def test_x64_registration_bounds_are_preserved():
    assert windows_timeouts.registration_limits('AMD64') == {
        'key_setup': 30, 'script': 105, 'pipe': 115, 'client': 140,
    }


@pytest.mark.parametrize('architecture,timeout', [('ARM64', 60), ('aarch64', 60), ('AMD64', 10)])
def test_service_identity_remains_required_with_architecture_deadline(monkeypatch, architecture, timeout):
    from agent.runtimes import windows_process
    monkeypatch.setattr(windows_timeouts.platform, 'machine', lambda: architecture)
    monkeypatch.setattr(windows_process, 'service_sid', lambda _: 'S-1-5-80-123')

    def correct_identity(args, **kwargs):
        assert kwargs['timeout'] == timeout
        return subprocess.CompletedProcess(args, 0, '"runner","S-1-5-80-123"', '')

    monkeypatch.setattr(registration.subprocess, 'run', correct_identity)
    registration.require_service_identity(RID)
    monkeypatch.setattr(registration.subprocess, 'run', lambda *a, **k:
                        subprocess.CompletedProcess(a[0], 0, '"SYSTEM","S-1-5-18"', ''))
    with pytest.raises(RuntimeError, match='own service virtual account'):
        registration.require_service_identity(RID)


def test_arm_pipe_leaves_room_for_the_service_to_start_before_the_script():
    """The pipe deadline runs from the moment the client starts waiting, so it
    has to hold the job host's start (PowerShell alone took 230 s under TCG)
    and then the whole script. At 915 s it did not: github-windows-arm64-1's
    recreate on 2026-10-08 failed with "registration service did not answer"
    as config.cmd was only starting. Everything still ends inside the
    controller's own deadline for the verb."""
    from .dashboard_deadline import CONTROLLER_DEADLINE
    arm = windows_timeouts.registration_limits('arm64')
    assert arm['pipe'] >= arm['script'] + 600
    assert arm['pipe'] < arm['client'] < CONTROLLER_DEADLINE
