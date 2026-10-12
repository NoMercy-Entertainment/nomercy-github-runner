import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "federation-started.js"
NODE = shutil.which("node") or ("C:/Program Files/nodejs/node.exe" if os.name == "nt" else None)
pytestmark = pytest.mark.skipif(not NODE or not Path(NODE).is_file(), reason="Node is unavailable")


def test_strict_event_policy_refuses_all_forks_unknown_and_unread_events():
    cases = [
        ("push", {"repository": {"id": 1, "fork": False}}, True),
        ("push", {"repository": {"id": 1, "fork": True}}, False),
        ("push", {}, False), ("unknown", {"repository": {"id": 1, "fork": False}}, False),
        ("pull_request", {"pull_request": {"head": {"repo": {"id": 1, "fork": False}}, "base": {"repo": {"id": 1}}}}, True),
        ("pull_request", {"pull_request": {"head": {"repo": {"id": 2, "fork": True}}, "base": {"repo": {"id": 1}}}}, False),
        ("pull_request", {"pull_request": {"head": {"repo": {"id": 2, "fork": False}}, "base": {"repo": {"id": 1}}}}, False),
        ("pull_request_target", {"repository": {"id": 1, "fork": False}}, False),
        ("workflow_run", {"repository": {"id": 1}, "workflow_run": {"pull_requests": [], "head_repository": {"id": 1, "fork": False}}}, True),
        ("workflow_run", {"repository": {"id": 1}, "workflow_run": {"pull_requests": [{"id": 8}], "head_repository": {"id": 1, "fork": False}}}, False),
    ]
    code = "const guard=require(process.argv[1]);const cases=JSON.parse(process.argv[2]);process.stdout.write(JSON.stringify(cases.map(c=>guard.allowed(c[0],c[1]))));"
    result = subprocess.run([NODE, "-e", code, str(HOOK), json.dumps(cases)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout) == [case[2] for case in cases]


def test_refusal_ends_only_its_real_owned_worker_and_leaves_listener_alive(tmp_path):
    # These two isolated Node executables simulate the real listener/worker
    # process boundary. No runner service or VM is contacted by this test.
    suffix = ".exe" if os.name == "nt" else ""
    worker, listener = tmp_path / ("Runner.Worker" + suffix), tmp_path / ("Runner.Listener" + suffix)
    shutil.copy2(NODE, worker)
    shutil.copy2(NODE, listener)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"repo": {"id": 2, "fork": True}}, "base": {"repo": {"id": 1}}}}))
    proof = tmp_path / "listener-alive.txt"
    survived = tmp_path / "worker-survived.txt"
    worker_code = "const child=require('child_process'),fs=require('fs');const r=child.spawnSync(process.argv[1],[process.argv[2]],{stdio:'inherit'});fs.writeFileSync(process.argv[3],'unsafe worker survived');process.exit(r.status===null?9:r.status);"
    listener_code = "const child=require('child_process'),fs=require('fs');const r=child.spawnSync(process.argv[1],['-e',process.argv[2],process.argv[3],process.argv[4],process.argv[6]],{stdio:'inherit'});fs.writeFileSync(process.argv[5],JSON.stringify({status:r.status,signal:r.signal}));"
    environment = dict(os.environ, GITHUB_EVENT_NAME="pull_request", GITHUB_EVENT_PATH=str(event))
    result = subprocess.run([str(listener), "-e", listener_code, str(worker), worker_code, NODE, str(HOOK), str(proof), str(survived)],
                            env=environment, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert "Forked or unverified" in result.stdout
    assert "could not" not in result.stdout
    ended = json.loads(proof.read_text())
    assert ended["signal"] == "SIGKILL" or ended["status"] != 0
    assert not survived.exists(), "A failed hook alone leaves always() steps executable; the job worker must end"
