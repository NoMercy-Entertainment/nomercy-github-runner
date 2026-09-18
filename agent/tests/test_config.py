"""A worker's configuration is checked whole before anything starts."""
import json

import pytest

from agent import __main__ as entry
from agent.config import ConfigError, load, parse

GOOD = {"host_id": "linux-worker-1", "runtime": "linux-container",
        "listen": "10.77.0.20:8443", "controller": "https://10.77.0.10:8444",
        "tls": {"cert": "a.crt", "key": "a.key", "ca": "ca.pem"}}


def ok(**changes):
    data = dict(GOOD, **changes)
    return parse({k: v for k, v in data.items() if v is not None},
                 exists=lambda p: True)


def refused(match, **changes):
    with pytest.raises(ConfigError, match=match):
        ok(**changes)


class TestAGoodOne:
    def test_it_parses(self):
        c = ok()
        assert (c.host_id, c.runtime, c.listen, c.controller) == (
            "linux-worker-1", "linux-container", ("10.77.0.20", 8443),
            "https://10.77.0.10:8444")
        assert c.permitted is None, "every verb unless it says otherwise"

    def test_a_trailing_slash_on_the_controller_is_not_a_path(self):
        assert ok(controller="https://10.77.0.10:8444/").controller == \
            "https://10.77.0.10:8444"

    def test_permitted_verbs_are_kept(self):
        c = ok(permitted=["exec_unit.status", "exec_unit.logs"])
        assert c.permitted == {"exec_unit.status", "exec_unit.logs"}


class TestWhatIsRefused:
    @pytest.mark.parametrize("name", ["host_id", "runtime", "listen",
                                      "controller", "tls"])
    def test_a_missing_field(self, name):
        refused(f"{name} is required", **{name: None})

    def test_a_field_it_does_not_take(self):
        refused("does not take", command="rm -rf /")

    def test_an_unknown_runtime(self):
        refused("runtime must be one of", runtime="kubernetes")

    @pytest.mark.parametrize("url", ["http://10.77.0.10:8444",
                                     "https://10.77.0.10:8444/v1/heartbeat",
                                     "10.77.0.10:8444"])
    def test_a_controller_that_is_not_plain_https(self, url):
        refused("https", controller=url)

    @pytest.mark.parametrize("listen", ["0.0.0.0:8443", "[::]:8443"])
    def test_listening_on_every_address(self, listen):
        refused("management address|address:port", listen=listen)

    @pytest.mark.parametrize("listen", ["worker:8443", "10.77.0.20",
                                        "10.77.0.20:0", "10.77.0.20:99999"])
    def test_a_listen_address_that_is_not_one(self, listen):
        refused("listen", listen=listen)

    def test_tls_naming_anything_but_the_three_files(self):
        refused("tls must name", tls={"cert": "a", "key": "b"})

    def test_a_tls_file_that_is_not_there(self):
        with pytest.raises(ConfigError, match="not found"):
            parse(GOOD, exists=lambda p: p != "a.key")

    def test_a_verb_that_is_not_in_the_protocol(self):
        refused("not protocol verbs", permitted=["exec_unit.shell"])

    def test_a_host_id_with_a_path_in_it(self):
        refused("not a worker name", host_id="../etc")


class TestReading:
    def test_a_file_that_is_not_json(self, tmp_path):
        path = tmp_path / "agent.json"
        path.write_text("{not json")
        with pytest.raises(ConfigError, match="not JSON"):
            load(str(path))

    def test_a_file_that_is_not_there(self, tmp_path):
        with pytest.raises(ConfigError, match="cannot read"):
            load(str(tmp_path / "missing.json"))

    def test_main_refuses_to_start_and_says_why(self, tmp_path, capsys):
        path = tmp_path / "agent.json"
        path.write_text(json.dumps(dict(GOOD, listen="0.0.0.0:8443")))
        assert entry.main(["--config", str(path)]) == 2
        assert "not started" in capsys.readouterr().err


class TestTheRuntimeItNames:
    @pytest.mark.parametrize("runtime,kind", [
        ("linux-container", "linux-container"),
        ("windows-process", "windows-process"),
        ("macos-appliance", "macos-appliance")])
    def test_each_is_built(self, runtime, kind):
        built, registrar = entry.runtime_for(ok(runtime=runtime))
        assert built.kind == kind
        assert hasattr(registrar, "register")

    def test_a_windows_tool_path_reaches_the_runtime(self):
        built, registrar = entry.runtime_for(ok(
            runtime="windows-process", tools={"nssm": r"D:\tools\nssm.exe"}))
        assert built._tools["nssm"] == r"D:\tools\nssm.exe"
        assert registrar._tools["nssm"] == r"D:\tools\nssm.exe"


class TestCapacity:
    def test_it_is_kept(self):
        c = ok(capacity={"max_runners": 2, "memory_bytes": 12 * 2 ** 30})
        assert c.capacity == {"max_runners": 2, "memory_bytes": 12 * 2 ** 30}

    @pytest.mark.parametrize("capacity", [
        {"max_runners": 0}, {"memory_bytes": "12g"}, {"max_runners": True},
        {"cpus": 4}, {"architecture": "sparc"}, ["max_runners"]])
    def test_what_is_not_a_capacity(self, capacity):
        refused("capacity", capacity=capacity)

    def test_it_is_added_to_what_the_runtime_declares(self):
        class Runtime:
            def capabilities(self):
                return {"kind": "linux-container", "supports_drain": True}

            def status(self, rid):
                return {"exists": True}

        declared = entry.Declared(Runtime(), {"max_runners": 2})
        assert declared.capabilities() == {
            "kind": "linux-container", "supports_drain": True,
            "max_runners": 2}
        assert declared.status("x") == {"exists": True}, \
            "everything else goes straight through"
