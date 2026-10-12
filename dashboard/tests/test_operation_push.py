import json
import shutil
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

class Socket:
    connected = True
    def __init__(self, sent=None):
        self.frames = []
        self.sent = sent
        self.closed = threading.Event()
    def receive(self):
        self.closed.wait()
        raise OSError("socket closed")
    def close(self):
        self.connected = False
        self.closed.set()
    def send(self, value):
        self.frames.append(json.loads(value))
        if self.sent:
            self.sent(self.frames[-1])

@pytest.fixture
def socket_context(tmp_path, monkeypatch):
    import app as dash
    monkeypatch.setattr(dash.users, "PATH", str(tmp_path / "users.json"))
    dash.users.approve("observer", "viewer")
    monkeypatch.setattr(dash, "_secret_values", lambda: ["sentinel-private-token"])
    monkeypatch.setattr(dash, "_public_url", lambda: "https://runner.example")
    return dash

def invoke(dash, socket, service, monkeypatch, sub="observer"):
    monkeypatch.setattr(dash.api_v2, "control_plane", lambda: (service, None))
    with dash.app.test_request_context("/ws/v2/operations/op-1", headers={"Origin": "https://runner.example"}):
        if sub:
            dash.session["sub"] = sub
        dash.app.view_functions["ws_operation_v2"].__wrapped__(socket, "op-1")

def test_initial_terminal_replay_is_redacted_and_closes(socket_context, monkeypatch):
    dash = socket_context
    op = {"operation_id": "op-1", "state": "succeeded", "verb": "clear_cache",
          "result": {"total_bytes": 5000000000}, "trace": ["sentinel-private-token"]}
    calls = []
    service = SimpleNamespace(operations=SimpleNamespace(get=lambda oid: calls.append(oid) or op))
    socket = Socket()
    invoke(dash, socket, service, monkeypatch)
    assert calls == ["op-1"]
    assert socket.frames[0]["data"]["state"] == "succeeded"
    assert "sentinel-private-token" not in json.dumps(socket.frames)
    assert socket.frames[0]["data"]["result"]["total_bytes"] == 5000000000

@pytest.mark.parametrize("sub,exists,expected", [
    (None, True, "not authenticated"),
    ("observer", False, "operation not found"),
])
def test_refusal_does_not_open_an_event_wait(socket_context, monkeypatch, sub, exists, expected):
    dash = socket_context
    service = SimpleNamespace(operations=SimpleNamespace(get=lambda oid: {} if exists else None))
    socket = Socket()
    invoke(dash, socket, service, monkeypatch, sub)
    assert socket.frames == [{"schema": dash.FLEET_SCHEMA, "type": "operation", "error": expected}]

def test_change_between_read_and_wait_is_not_lost(socket_context, monkeypatch):
    dash = socket_context
    running = {"operation_id": "op-1", "verb": "restart", "state": "running"}
    finished = dict(running, state="succeeded")
    reads = []
    def read(oid):
        reads.append(oid)
        return running if len(reads) == 1 else finished
    def sent(frame):
        if frame.get("data", {}).get("state") == "running":
            with dash._status_lock:
                dash._status_gen += 1
                dash._status_lock.notify_all()
    socket = Socket(sent)
    invoke(dash, socket, SimpleNamespace(operations=SimpleNamespace(get=read)), monkeypatch)
    assert [f["data"]["state"] for f in socket.frames] == ["running", "succeeded"]
    assert reads == ["op-1", "op-1"]

def test_native_notification_delivers_completion_without_repeated_reads(socket_context, monkeypatch):
    dash = socket_context
    first = threading.Event()
    finished = threading.Event()
    reads = []
    current = {"operation_id": "op-1", "verb": "restart", "state": "running"}
    def read(oid):
        reads.append(oid)
        return dict(current)
    socket = Socket(lambda frame: first.set())
    service = SimpleNamespace(operations=SimpleNamespace(get=read))
    monkeypatch.setattr(dash.api_v2, "control_plane", lambda: (service, None))
    def serve():
        with dash.app.test_request_context("/ws/v2/operations/op-1", headers={"Origin": "https://runner.example"}):
            dash.session["sub"] = "observer"
            dash.app.view_functions["ws_operation_v2"].__wrapped__(socket, "op-1")
        finished.set()
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert first.wait(2)
        assert not finished.wait(.08)
        assert reads == ["op-1"]
        current["state"] = "failed"
        current["error"] = "sentinel-private-token"
        with dash._status_lock:
            dash._status_gen += 1
            dash._status_lock.notify_all()
        assert finished.wait(2)
        assert len(reads) == 2
        assert socket.frames[-1]["data"]["state"] == "failed"
        assert "sentinel-private-token" not in json.dumps(socket.frames)
    finally:
        socket.close()
        thread.join(2)
    assert not thread.is_alive()

def test_quiet_socket_disconnect_wakes_without_database_mutation(socket_context, monkeypatch):
    dash = socket_context
    first = threading.Event()
    finished = threading.Event()
    reads = []
    operation = {"operation_id": "op-1", "verb": "restart", "state": "running"}
    service = SimpleNamespace(operations=SimpleNamespace(get=lambda oid: reads.append(oid) or operation))
    monkeypatch.setattr(dash.api_v2, "control_plane", lambda: (service, None))
    socket = Socket(lambda frame: first.set())
    def serve():
        with dash.app.test_request_context("/ws/v2/operations/op-1", headers={"Origin": "https://runner.example"}):
            dash.session["sub"] = "observer"
            dash.app.view_functions["ws_operation_v2"].__wrapped__(socket, "op-1")
        finished.set()
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert first.wait(2)
        assert not finished.wait(.08)
        with dash._status_lock:
            generation = dash._status_gen
        socket.close()  # No generation change or dashboard notification here.
        assert finished.wait(2)
        assert dash._status_gen == generation
        assert reads == ["op-1"]
    finally:
        socket.close()
        thread.join(2)
    assert not thread.is_alive()


def test_role_revocation_blocks_later_operation_contents(socket_context, monkeypatch):
    dash = socket_context
    calls = []
    def read(oid):
        calls.append(oid)
        return {"operation_id": oid, "verb": "restart", "state": "running"}
    def revoke(frame):
        if frame.get("data"):
            dash.users.revoke("observer")
            with dash._status_lock:
                dash._status_gen += 1
                dash._status_lock.notify_all()
    socket = Socket(revoke)
    invoke(dash, socket, SimpleNamespace(operations=SimpleNamespace(get=read)), monkeypatch)
    assert calls == ["op-1"]
    assert socket.frames[-1] == {"schema": dash.FLEET_SCHEMA, "type": "operation", "error": "not authenticated"}

@pytest.mark.parametrize("origin", [None, "null", "https://untrusted.example"])
def test_socket_origin_is_pinned_before_operation_read(socket_context, monkeypatch, origin):
    dash = socket_context
    def forbidden_read():
        raise AssertionError("foreign origin reached the control plane")
    monkeypatch.setattr(dash.api_v2, "control_plane", forbidden_read)
    socket = Socket()
    headers = {"Origin": origin} if origin else {}
    with dash.app.test_request_context("/ws/v2/operations/op-1", headers=headers):
        dash.session["sub"] = "observer"
        dash.app.view_functions["ws_operation_v2"].__wrapped__(socket, "op-1")
    assert socket.frames == [{"schema": dash.FLEET_SCHEMA, "type": "operation", "error": "origin not allowed"}]

def test_anonymous_websocket_handshake_is_rejected(anon_client):
    response = anon_client.get("/ws/v2/operations/op-1", environ_overrides={"wsgi.websocket": True},
                               headers={"Origin": "https://runner.example"})
    assert response.status_code == 401

def test_browser_receives_and_reconnects_without_operation_requests():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node required for browser socket behavior")
    program = r"""
const fs = require('fs'), vm = require('vm'), assert = require('assert');
const sockets=[], reports=[], timers=[];
const context={
 location:{protocol:'https:',host:'runner.example'},
 window:{addEventListener(){},removeEventListener(){}},
 WebSocket:class {constructor(url){this.url=url;sockets.push(this)} close(){if(this.onclose)this.onclose()}},
 fetch(){throw new Error('Operation API polling is forbidden')},
 setTimeout(fn){timers.push(fn);return timers.length},clearTimeout(){},
 console,JSON,Promise,Error,encodeURIComponent,Number
};
vm.createContext(context);vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
(async()=>{
 const promise=context.watchOperation('op-1',(...args)=>reports.push(args));
 assert.equal(sockets.length,1);
 sockets[0].onmessage({data:JSON.stringify({type:'operation',data:{verb:'clear_cache',state:'running'}})});
 assert.equal(reports.length,1);
 sockets[0].onclose();assert.equal(timers.length,1);timers.shift()();
 assert.equal(sockets.length,2);
 sockets[1].onmessage({data:JSON.stringify({type:'operation',data:{verb:'clear_cache',state:'succeeded',result:JSON.stringify({total_bytes:5e9})}})});
 const op=await promise;assert.equal(op.state,'succeeded');
 assert(reports[1][0].includes('freed 5.0 GB'));assert.equal(timers.length,0);
 const denied=context.watchOperation('op-2',()=>{throw Error('denied data was reported')});
 sockets[2].onmessage({data:JSON.stringify({type:'operation',error:'not authenticated'})});
 await assert.rejects(denied,/not authenticated/);
 assert.equal(timers.length,0);
})().catch(error=>{console.error(error);process.exitCode=1});
"""
    result = subprocess.run([node, "-e", program, str(Path(__file__).parents[1] / "templates/_v2_push.js")],
                            text=True,capture_output=True,timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
