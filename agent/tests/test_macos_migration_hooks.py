import ast
from pathlib import Path

import pytest

from agent.runtimes.macos_appliance import MacApplianceRuntime
from .fake_macos import FakeMac, TEMPLATE, TOOLS


@pytest.mark.parametrize('provider', ['github', 'forgejo'])
def test_migration_installs_provider_hooks_without_starting_registered_listener(provider):
    script = Path(__file__).resolve().parents[2] / 'infra/hyperv/migrate-uniform-macos.py'
    tree = ast.parse(script.read_text())
    function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'target_spec')
    namespace = {'TEMPLATES': (TEMPLATE,), 'GIB': 1024**3}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(script), 'exec'), namespace)
    spec = namespace['target_spec'](0, {'expected': {'provider': provider}, 'environment': {}})
    guest = FakeMac()
    runtime = MacApplianceRuntime(run=guest, fs=guest, appliance=guest.appliance, tools=TOOLS)
    rid = '3f2504e0-4f89-41d3-9a0c-0305e82c3301'
    registration = runtime.paths(rid)['reg'] + '/.runner'
    guest.makedirs(runtime.paths(rid)['reg'])
    guest.write_text(registration, 'retained-registration')
    runtime.create(rid, spec, start=False)
    report = runtime.origin_guard_report(rid)
    assert (report and report['version'] == 1) if provider == 'github' else report is None
    assert guest.read_text(registration) == 'retained-registration'
    assert not runtime.status(rid)['running']
