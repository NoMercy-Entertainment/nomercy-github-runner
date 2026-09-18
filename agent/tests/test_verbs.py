"""The agent can only be asked for named things (T-0401, NFR-10).

The last class is the enforcement the plan asks for: it reads every source file
in the agent and fails on any process-spawning call whose arguments are not a
literal list, and on any use of a shell. A test of today's handlers would show
they are safe today; this shows the next handler cannot be made unsafe without
the suite noticing.

Everything else checks the closed surface from the outside, over real HTTP: an
unknown verb is refused before its body is read, a known verb refuses any field
it does not take, and every value is validated before a runtime sees it.
"""
import ast
import http.client
import inspect
import json
import os
import re
import socket
from types import MappingProxyType

import pytest

from agent import protocol, verbs
from agent.server import AgentServer
from agent.verbs import VERBS, Agent, Refused, dispatch

from .fakes import FakeRegistrar, FakeRuntime

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def runtime():
    return FakeRuntime()


@pytest.fixture
def registrar():
    return FakeRegistrar()


@pytest.fixture
def agent(runtime, registrar):
    return Agent("linux-1", runtime, registrar, version="0.1")


@pytest.fixture
def server(agent):
    s = AgentServer(agent).start()
    yield s
    s.stop()


def post(server, verb, body=None, raw=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    data = raw if raw is not None else json.dumps(body or {}).encode()
    conn.request("POST", protocol.OP_PATH + verb, body=data,
                 headers=dict({"Content-Type": "application/json"},
                              **(headers or {})))
    response = conn.getresponse()
    payload = json.loads(response.read() or b"{}")
    conn.close()
    return response.status, payload


class TestTheTableIsClosed:
    def test_it_holds_exactly_the_designs_verbs(self):
        assert set(VERBS) == protocol.VERB_NAMES

    def test_it_cannot_be_changed_at_run_time(self):
        assert isinstance(VERBS, MappingProxyType)
        with pytest.raises(TypeError):
            VERBS["run"] = lambda agent, body: None

    def test_every_verb_declares_its_fields(self):
        assert set(verbs.FIELDS) == protocol.VERB_NAMES

    def test_no_verb_takes_a_command_a_path_or_a_shell(self):
        banned = {"command", "cmd", "args", "argv", "shell", "script",
                  "path", "mount", "mounts", "volume", "volumes",
                  "entrypoint", "exec", "run"}
        for verb, fields in verbs.FIELDS.items():
            assert not (fields & banned), verb
        assert not (verbs.SPEC_FIELDS & banned)
        assert not (verbs.PLAN_FIELDS & banned)

    def test_every_handler_takes_the_agent_and_the_body_and_nothing_else(
            self):
        """The plan's "every handler signature is asserted"."""
        for verb, handler in VERBS.items():
            params = list(inspect.signature(handler).parameters)
            assert params == ["agent", "body"], verb

    def test_heartbeat_and_event_are_not_things_an_agent_answers(self):
        """They go the other way, agent to controller."""
        assert "heartbeat" not in VERBS and "event" not in VERBS


class TestAnUnknownVerbIsRefusedBeforeAnyParsing:
    def test_it_is_a_404(self, server):
        assert post(server, "run", {"command": "id"})[0] == 404

    def test_the_body_is_never_read(self, server):
        """The body here is not JSON and is shorter than its Content-Length
        claims. A server that tried to read it would wait for bytes that never
        come, then fail to parse them; this one answers from the path."""
        sock = socket.create_connection(("127.0.0.1", server.port), timeout=3)
        sock.sendall(b"POST /v1/op/shell HTTP/1.1\r\nHost: x\r\n"
                     b"Content-Length: 100000\r\n\r\n{{{not json")
        answer = sock.recv(4096)
        sock.close()
        assert answer.startswith(b"HTTP/1.0 404") or \
            answer.startswith(b"HTTP/1.1 404")

    def test_a_path_outside_the_op_tree_is_a_404(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("POST", "/admin", body=b"{}")
        assert conn.getresponse().status == 404

    def test_only_post_is_served(self, server):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
        conn.request("GET", protocol.OP_PATH + "hello")
        assert conn.getresponse().status == 405

    def test_dispatch_refuses_it_too(self, agent):
        """Defence in depth: the table is consulted again at dispatch."""
        with pytest.raises(Refused) as caught:
            dispatch(agent, "exec_unit.shell", {})
        assert caught.value.status == 404


class TestAKnownVerbTakesOnlyItsOwnFields:
    def test_an_extra_field_is_refused_not_ignored(self, server, runtime):
        """Ignored today is honoured by accident tomorrow."""
        status, payload = post(server, "exec_unit.start",
                               {"runner_id": RID, "command": "rm -rf /"})
        assert status == 400
        assert "command" in payload["error"]
        assert runtime.calls == []

    def test_a_spec_cannot_carry_a_mount(self, server, runtime):
        """Where data lives is derived on the worker from the runner_id."""
        status, _ = post(server, "exec_unit.create",
                         {"runner_id": RID,
                          "spec": {"image": "node:20",
                                   "mounts": ["/:/host"]}})
        assert status == 400
        assert runtime.calls == []

    @pytest.mark.parametrize("field", ["privileged", "entrypoint", "devices",
                                       "cap_add", "network_mode", "user"])
    def test_a_spec_cannot_change_how_the_unit_runs(self, server, field):
        status, _ = post(server, "exec_unit.create",
                         {"runner_id": RID,
                          "spec": {"image": "node:20", field: True}})
        assert status == 400

    def test_a_refusal_names_the_field_but_never_its_value(self, server):
        """A field sent in the wrong place may be a token."""
        status, payload = post(server, "runner.deregister",
                               {"runner_id": RID,
                                "token": "tok-SENTINEL-99887766"})
        assert status == 400
        assert "token" in payload["error"]
        assert "SENTINEL" not in payload["error"]


class TestEveryValueIsValidated:
    @pytest.mark.parametrize("rid", ["../../etc", "github-runner-1", "",
                                     f"{RID}/../x", 42, None])
    def test_the_runner_id_must_be_a_uuid(self, server, runtime, rid):
        status, _ = post(server, "exec_unit.stop", {"runner_id": rid})
        assert status == 400
        assert runtime.calls == []

    @pytest.mark.parametrize("probe", ["df -h", "disk_usage; id",
                                       "DISK_USAGE", "", None])
    def test_a_probe_is_a_name_from_the_closed_set(self, server, probe):
        status, _ = post(server, "exec_unit.probe",
                         {"runner_id": RID, "probe": probe})
        assert status == 400

    def test_every_named_probe_is_accepted(self, server):
        for probe in protocol.PROBES:
            assert post(server, "exec_unit.probe",
                        {"runner_id": RID, "probe": probe})[0] == 200

    @pytest.mark.parametrize("env", [{"PATH;id": "x"}, {"lower": "x"},
                                     {"A": 1}, {"A": "a\x00b"}])
    def test_environment_names_and_values_are_checked(self, server, env):
        status, _ = post(server, "exec_unit.create",
                         {"runner_id": RID,
                          "spec": {"image": "node:20", "env": env}})
        assert status == 400

    @pytest.mark.parametrize("plan_change", [
        {"url": "https://x$(id)"}, {"url": "file:///etc/passwd"},
        {"token": "has space"}, {"token": ""},
        {"labels": "a;rm -rf /"}, {"name": "x`id`"},
    ])
    def test_a_registration_plan_is_checked(self, server, registrar,
                                            plan_change):
        plan = dict({"url": "https://git.example", "token": "tok-123456789",
                     "name": "rnr-3f2504e0", "labels": "self-hosted"},
                    **plan_change)
        status, _ = post(server, "runner.register",
                         {"runner_id": RID, "plan": plan})
        assert status == 400
        assert registrar.calls == []

    def test_a_cache_clear_cannot_name_a_scope_that_is_not_the_runners(
            self, server, runtime):
        status, _ = post(server, "exec_unit.clear_cache",
                         {"runner_id": RID,
                          "policy": {"scopes": ["engine-build-cache",
                                                "/var/lib/docker"]}})
        assert status == 400
        assert runtime.calls == []

    def test_keep_data_must_be_a_boolean(self, server):
        status, _ = post(server, "exec_unit.remove",
                         {"runner_id": RID, "keep_data": "yes"})
        assert status == 400

    def test_the_log_window_is_bounded(self, server):
        status, _ = post(server, "exec_unit.logs",
                         {"runner_id": RID, "since_seconds": 10 ** 9})
        assert status == 400


class TestWhatReachesTheRuntimeIsTheCheckedValue:
    def test_create_passes_the_validated_spec(self, server, runtime):
        status, payload = post(server, "exec_unit.create", {
            "runner_id": RID.upper(),
            "spec": {"image": "ghcr.io/nomercy/runner:latest",
                     "env": {"RUNNER_LABELS": "self-hosted"},
                     "cpuset": "0-15", "memory": "32g"}})
        assert status == 200
        assert payload["result"]["handle"] == f"rnr-{RID}"
        assert runtime.calls[0][1] == RID, "the id is normalised"
        assert runtime.calls[0][2]["cpuset"] == "0-15"

    def test_remove_passes_keep_data(self, server, runtime):
        post(server, "exec_unit.remove", {"runner_id": RID,
                                          "keep_data": True})
        assert runtime.calls == [("remove", RID, True)]

    def test_hello_says_who_and_what(self, server):
        status, payload = post(server, "hello")
        assert payload["result"] == {"host_id": "linux-1",
                                     "agent_version": "0.1",
                                     "protocol_major":
                                         protocol.PROTOCOL_MAJOR}

    def test_register_returns_the_forges_ids_and_not_the_plan(
            self, server, registrar):
        """The token goes in and does not come back out."""
        status, payload = post(server, "runner.register", {
            "runner_id": RID,
            "plan": {"url": "https://git.example",
                     "token": "tok-SENTINEL-12345678",
                     "labels": "docker:docker://node:20"}})
        assert status == 200
        assert set(payload["result"]) == {"registration_id",
                                          "registration_uuid"}
        assert "SENTINEL" not in json.dumps(payload)
        assert registrar.calls[0][2]["token"] == "tok-SENTINEL-12345678"


class TestAFailureInsideTheAgentStaysInside:
    def test_a_runtime_error_is_a_500_without_its_message(self, registrar):
        """An exception message can carry anything the runtime held,
        including a token."""
        agent = Agent("linux-1", FakeRuntime(raise_with="leaked tok-ABCDEFGH"),
                      registrar)
        s = AgentServer(agent).start()
        try:
            status, payload = post(s, "exec_unit.start", {"runner_id": RID})
        finally:
            s.stop()
        assert status == 500
        assert "tok-ABCDEFGH" not in json.dumps(payload)

    def test_a_body_that_is_not_json_is_a_400(self, server):
        assert post(server, "hello", raw=b"{not json")[0] == 400

    def test_a_body_that_is_not_an_object_is_a_400(self, server):
        assert post(server, "hello", raw=b"[1, 2]")[0] == 400

    def test_an_enormous_body_is_refused_unread(self, server):
        status, _ = post(server, "hello", raw=b"{}",
                         headers={"Content-Length": str(10 ** 9)})
        assert status == 413


# ---------------------------------------------------------------------------
# the enforcement
# ---------------------------------------------------------------------------

SPAWNERS = {("subprocess", "run"), ("subprocess", "Popen"),
            ("subprocess", "call"), ("subprocess", "check_call"),
            ("subprocess", "check_output"), ("os", "system"),
            ("os", "popen"), ("os", "execv"), ("os", "execvp"),
            ("os", "spawnv"), ("os", "spawnl")}
#: These take a shell string by their nature, so no argument form makes them
#: acceptable.
ALWAYS_BANNED = {("os", "system"), ("os", "popen")}


def unsafe_spawns(source, filename="<source>"):
    """Process-spawning calls whose command is not a literal list, and any
    use of a shell. Returns descriptions; empty means clean."""
    found = []
    if "Invoke-Expression" in source:
        found.append(f"{filename}: mentions Invoke-Expression")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)):
            continue
        key = (func.value.id, func.attr)
        if key not in SPAWNERS:
            continue
        where = f"{filename}:{node.lineno} {key[0]}.{key[1]}"
        if key in ALWAYS_BANNED:
            found.append(f"{where} runs a shell string")
            continue
        if not node.args or not isinstance(node.args[0], ast.List):
            found.append(f"{where} is not given a literal list")
        for kw in node.keywords:
            if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant)
                                          and kw.value.value is False):
                found.append(f"{where} asks for a shell")
    return found


def agent_sources():
    for root, dirs, files in os.walk(AGENT_DIR):
        dirs[:] = [d for d in dirs if d not in ("tests", "__pycache__")]
        for name in files:
            if name.endswith((".py", ".ps1", ".sh")):
                path = os.path.join(root, name)
                yield path, open(path, encoding="utf-8").read()


class TestNothingInTheAgentCanRunAnArbitraryCommand:
    """NFR-10. "The test above is the enforcement, not the intention." """

    def test_no_file_spawns_a_process_from_anything_but_a_literal_list(self):
        offenders = []
        for path, source in agent_sources():
            if path.endswith(".py"):
                offenders += unsafe_spawns(source, os.path.basename(path))
            elif "Invoke-Expression" in source or re.search(
                    r"(?i)iex", source):
                # PowerShell's alias for it counts too.
                offenders.append(f"{os.path.basename(path)}: "
                                 f"Invoke-Expression")
        assert offenders == []

    @pytest.mark.parametrize("bad", [
        "import os\nos.system('id')\n",
        "import os\nos.popen('id')\n",
        "import subprocess\nsubprocess.run('id', shell=True)\n",
        "import subprocess\nsubprocess.run(cmd)\n",
        "import subprocess\nsubprocess.run(['id'], shell=True)\n",
        "import subprocess\nsubprocess.Popen(' '.join(parts))\n",
        "x = 'Invoke-Expression $y'\n",
    ])
    def test_the_check_catches_each_unsafe_form(self, bad):
        """A check that cannot fail proves nothing."""
        assert unsafe_spawns(bad)

    def test_the_check_allows_a_literal_list(self):
        good = "import subprocess\nsubprocess.run(['docker', 'ps'])\n"
        assert unsafe_spawns(good) == []


class TestTheAgentStandsAlone:
    def test_it_imports_nothing_from_the_dashboard(self):
        """It is installed on workers that do not have the dashboard."""
        dashboard = os.path.join(os.path.dirname(AGENT_DIR), "dashboard")
        theirs = {os.path.splitext(n)[0] for n in os.listdir(dashboard)
                  if n.endswith(".py")}
        theirs |= {n for n in os.listdir(dashboard)
                   if os.path.isdir(os.path.join(dashboard, n))
                   and n not in ("tests", "templates", "__pycache__")}
        for path, source in agent_sources():
            if not path.endswith(".py"):
                continue
            for node in ast.walk(ast.parse(source)):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module \
                        and node.level == 0:
                    names = [node.module.split(".")[0]]
                assert not (set(names) & theirs), (
                    f"{os.path.basename(path)} imports {names}")
