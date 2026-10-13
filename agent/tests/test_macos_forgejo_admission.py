import plistlib
import pytest

from agent.runtimes.macos_appliance import MacApplianceRuntime
from agent.runtimes.macos_forgejo_admission import install, program

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def test_policy_uses_protected_launcher_and_keeps_registration_resource_layout():
    runtime = MacApplianceRuntime(tools={'domain':'system','runner_user':'user'})
    paths = runtime.paths(RID)
    spec = {'env':{'NOMERCY_FORGEJO_ADMISSION_POLICY':'protected-policy'}}
    doc = plistlib.loads(runtime._plist(RID, spec, paths).encode())
    assert doc['ProgramArguments'] == [program(RID)]
    assert doc['EnvironmentVariables']['NOMERCY_FORGEJO_ADMISSION_POLICY'] == 'protected-policy'
    assert doc['WorkingDirectory'] == paths['work'] and doc['UserName'] == 'user'
    old = plistlib.loads(runtime._plist(RID, {}, paths).encode())
    assert old['ProgramArguments'] == [paths['reg'] + '/run']


def test_missing_reviewed_binary_fails_before_a_guest_command():
    runtime = MacApplianceRuntime(run=lambda *a, **kw: pytest.fail('no guest execution'))
    with pytest.raises(RuntimeError, match='protected artifact'):
        install(runtime, RID)


def test_invalid_runner_identity_cannot_select_another_protected_launcher():
    with pytest.raises(ValueError):
        program('../outside')
