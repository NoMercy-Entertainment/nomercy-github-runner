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


def run_launcher(tmp_path, monkeypatch, toolcache=None, runner="{}"):
    import json
    from agent.runtimes.macos_forgejo_admission import LAUNCHER
    if toolcache is None:
        monkeypatch.delenv("RUNNER_TOOL_CACHE", raising=False)
    else:
        monkeypatch.setenv("RUNNER_TOOL_CACHE", toolcache)
    reg = tmp_path / "rid" / "reg"
    reg.mkdir(parents=True)
    (reg / ".runner").write_text(runner)
    calls = []
    source = (LAUNCHER.replace("REG", repr(str(reg))).replace("BINARY", repr("/bin/true"))
              .replace("os.execv(", "called("))
    exec(compile(source, "launcher", "exec"),
         {"called": lambda *a: calls.append(a), "__name__": "__main__"})
    assert calls
    return json.loads((reg / "config.yaml").read_text())["runner"]["envs"]


def test_launcher_gives_forgejo_the_runner_tool_cache_and_a_home(tmp_path, monkeypatch):
    """Forgejo's host mode keeps its tool cache in <workdir>/tool_cache unless
    the runner config names one, while the Python installer writes where
    AGENT_TOOLSDIRECTORY points. A version one job installed was never found
    by the next, and Python was installed again on every macOS job
    (2026-10-27)."""
    envs = run_launcher(tmp_path, monkeypatch)
    toolcache = str(tmp_path / "rid" / "cache" / "toolcache")
    assert envs["RUNNER_TOOL_CACHE"] == envs["AGENT_TOOLSDIRECTORY"] == toolcache
    assert (tmp_path / "rid" / "cache" / "toolcache").is_dir()
    assert (tmp_path / "rid" / "work" / ".home").is_dir()


def test_launcher_keeps_the_tool_cache_its_launchd_job_names(tmp_path, monkeypatch):
    hosted = tmp_path / "hostedtoolcache"
    envs = run_launcher(tmp_path, monkeypatch, toolcache=str(hosted))
    assert envs["RUNNER_TOOL_CACHE"] == envs["AGENT_TOOLSDIRECTORY"] == str(hosted)
    assert hosted.is_dir()


def test_launcher_tells_jobs_the_runner_name(tmp_path, monkeypatch):
    """Forgejo's host mode leaves RUNNER_NAME empty in a job (2026-10-27);
    the name it registered with is in .runner."""
    envs = run_launcher(tmp_path, monkeypatch,
                        runner='{"id": 52, "name": "forgejo-macos-x64-1", "uuid": "x"}')
    assert envs["RUNNER_NAME"] == "forgejo-macos-x64-1"


def test_launcher_without_a_name_sets_none(tmp_path, monkeypatch):
    assert "RUNNER_NAME" not in run_launcher(tmp_path, monkeypatch)
