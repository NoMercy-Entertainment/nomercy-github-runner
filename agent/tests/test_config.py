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
    def test_windows_short_workspace_option_is_accepted(self):
        config = ok(runtime="windows-process", tools={"short_workspaces": r"D:\w"})
        assert config.tools["short_workspaces"] == r"D:\w"

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


class TestAppliancePowerConfiguration:
    GUEST = {"host": "127.0.0.1", "user": "user", "key": "guest.key"}

    def test_only_closed_fields_are_accepted(self):
        with pytest.raises(ConfigError, match="appliance"):
            ok(runtime="macos-appliance", guest=self.GUEST,
               appliance={"name": "macos-sequoia", "command": "anything"})

    def test_a_local_guest_has_no_host_power_control(self):
        with pytest.raises(ConfigError, match="guest connection"):
            ok(runtime="macos-appliance", appliance={"name": "macos-sequoia"})

    @pytest.mark.parametrize("name", ["*", "-f", "a/b", "a b", "$(id)"])
    def test_one_exact_unit_must_be_named(self, name):
        with pytest.raises(ConfigError, match="appliance.name"):
            ok(runtime="macos-appliance", guest=self.GUEST, appliance={"name": name})

    def test_power_control_is_attached_to_remote_runtime(self):
        config = ok(runtime="macos-appliance", guest=self.GUEST,
                    appliance={"name": "macos-sequoia", "boot_timeout": 300})
        runtime, _ = entry.runtime_for(config)
        assert runtime._appliance.name == "macos-sequoia"
        assert runtime._appliance._timeout == 300

    def test_system_domain_requires_a_runner_user(self):
        with pytest.raises(ConfigError, match="runner_user"):
            ok(runtime="macos-appliance", tools={"domain": "system"})

    def test_unknown_tool_names_do_not_silently_do_nothing(self):
        with pytest.raises(ConfigError, match="tools"):
            ok(tools={"shell": "some-command"})


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


class TestAppliancePoolConfig:
    POOL = {"image": "sha256:" + "a" * 64, "base_disk": "/srv/base/macos.qcow2",
            "base_system": "/srv/base/BaseSystem.img", "data_root": "/srv/instances",
            "templates": ["github-macos", "forgejo-macos"], "base_guests_disabled": True}
    GUEST = {"host": "127.0.0.1", "user": "runner", "key": "guest.key"}

    def config(self, pool=None, **changes):
        values = dict(runtime="macos-appliance", guest=self.GUEST, tools={"domain": "gui/501"},
                      appliance_pool=self.POOL if pool is None else pool)
        values.update(changes)
        return ok(**values)

    def test_explicit_opt_in_with_bounded_default_capacity(self):
        config = self.config()
        assert config.appliance_pool == self.POOL
        assert config.capacity == {"max_runners": 2, "memory_bytes": 24 * 1024 ** 3}
        assert ok(runtime="macos-appliance").appliance_pool == {}

    @pytest.mark.parametrize("patch", [{"image": "some:latest"}, {"base_guests_disabled": False},
                                       {"templates": ["../../unsafe"]}, {"ssh_port_base": True},
                                       {"base_disk": "relative.qcow2"}, {"data_root": "/srv/x,y"},
                                       {"command": "evil"}, {"boot_timeout": 0}])
    def test_invalid_pool_config_is_rejected(self, patch):
        with pytest.raises(ConfigError, match="appliance_pool"):
            self.config(dict(self.POOL, **patch))

    def test_pool_and_legacy_appliance_cannot_both_own_the_guest(self):
        with pytest.raises(ConfigError, match="excludes appliance"):
            self.config(appliance={"name": "legacy"})

    def test_linux_host_required_before_runtime_creation(self, monkeypatch):
        monkeypatch.setattr(entry.sys, "platform", "win32")
        with pytest.raises(ConfigError, match="Linux KVM"):
            entry.runtime_for(self.config())

    def test_pool_runtime_gets_fixed_guest_credentials(self, monkeypatch):
        from agent.runtimes import macos_pool
        seen = {}

        class Pool:
            def __init__(self, **kwargs):
                seen.update(kwargs)

        monkeypatch.setattr(entry.sys, "platform", "linux")
        monkeypatch.setattr(macos_pool, "MacAppliancePoolRuntime", Pool)
        runtime, registrar = entry.runtime_for(self.config())
        assert registrar.pool is runtime
        assert seen["guest"]["key"] == "guest.key"
        assert seen["image"] == self.POOL["image"]


class TestDiskStorageConfig:
    def test_linux_storage_is_explicit_and_default_bounded(self):
        assert ok().storage == {}
        assert ok(storage={"root": "/var/lib/runner-storage"}).storage == {
            "root": "/var/lib/runner-storage", "default_bytes": 100 * 1024 ** 3,
            "readonly_root": True}

    def test_readonly_root_is_explicit_and_defaults_true(self):
        assert ok(storage={"root": "/safe"}).storage["readonly_root"] is True
        assert ok(storage={"root": "/safe", "readonly_root": False}).storage == {
            "root": "/safe", "default_bytes": 100 * 1024 ** 3, "readonly_root": False}

    @pytest.mark.parametrize("storage", [{"root": "/"}, {"root": "relative"},
                                          {"root": "/safe", "default_bytes": True},
                                          {"root": "/safe", "default_bytes": 1},
                                          {"root": "/safe", "default_bytes": 2 ** 63},
                                          {"root": "/safe", "readonly_root": "yes"},
                                          {"root": "/safe", "readonly_root": 1},
                                          {"root": "/safe", "command": "evil"}])
    def test_linux_bad_config_rejected(self, storage):
        with pytest.raises(ConfigError, match="storage"):
            ok(storage=storage)

    def test_unknown_storage_key_is_refused_by_name(self):
        refused("storage may name only root, default_bytes and readonly_root",
                storage={"root": "/safe", "bogus": 1})

    def test_wrong_runtime_rejected(self):
        with pytest.raises(ConfigError, match="only by linux-container"):
            ok(runtime="windows-process", storage={"root": "/safe"})

    def test_windows_storage_is_explicit_and_default_bounded(self):
        assert ok(runtime="windows-process").windows_storage == {}
        result = ok(runtime="windows-process", windows_storage={
            "enabled": True, "root": "D:/runner-disks"}).windows_storage
        assert result["default_limit"] == 100 * 1024 ** 3
        assert result["reserve_bytes"] == 20 * 1024 ** 3

    @pytest.mark.parametrize("patch", [{"enabled": "yes"}, {"root": "D:/"},
                                       {"root": "//server/share"}, {"root": "/relative"},
                                       {"reserve_bytes": -1}, {"default_limit": False},
                                       {"default_limit": 1}, {"default_limit": 1024 ** 3 + 1}])
    def test_windows_bad_config_rejected(self, patch):
        with pytest.raises(ConfigError, match="windows_storage"):
            ok(runtime="windows-process", windows_storage=dict(
                {"enabled": True, "root": "D:/runner-disks"}, **patch))


class TestTheGuestOfAnAppliance:
    """An appliance's agent runs on the appliance host, not inside the guest
    (the worker kind the store names `macos-appliance-host`). `guest` says
    how to reach the guest from there; without it the agent is inside the
    guest and acts on its own disk."""

    GUEST = {"host": "127.0.0.1", "port": 50922, "user": "user",
             "password_file": "guest.pass"}

    def mac(self, **changes):
        return ok(runtime="macos-appliance",
                  guest=dict(self.GUEST, **changes))

    def test_it_is_kept_whole(self):
        c = self.mac()
        assert c.guest["host"] == "127.0.0.1"
        assert c.guest["port"] == 50922
        assert c.guest["user"] == "user"

    def test_without_it_the_agent_is_inside_the_guest(self):
        assert ok(runtime="macos-appliance").guest == {}

    def test_only_an_appliance_has_one(self):
        refused("guest", runtime="linux-container", guest=self.GUEST)

    def test_it_must_say_how_to_authenticate(self):
        with pytest.raises(ConfigError, match="key or password_file"):
            ok(runtime="macos-appliance",
               guest={"host": "127.0.0.1", "user": "user"})

    def test_a_credential_that_is_not_there_stops_the_agent(self):
        with pytest.raises(ConfigError, match="guest password_file"):
            parse(dict(GOOD, runtime="macos-appliance", guest=self.GUEST),
                  exists=lambda p: p != "guest.pass")

    @pytest.mark.parametrize("guest", [
        {"host": "h", "user": "u", "key": "k", "port": 0},
        {"host": "h", "user": "u", "key": "k", "port": "fifty"},
        {"user": "u", "key": "k"},
        {"host": "h", "key": "k"},
        {"host": "h", "user": "u", "key": "k", "shell": "/bin/sh"},
        ["host"]])
    def test_what_is_not_a_guest(self, guest):
        refused("guest", runtime="macos-appliance", guest=guest)

    def test_the_runtime_acts_inside_the_guest(self, tmp_path):
        secret = tmp_path / "guest.pass"
        secret.write_text("alpine\n", encoding="utf-8")
        c = parse(dict(GOOD, runtime="macos-appliance",
                       guest=dict(self.GUEST,
                                  password_file=str(secret))),
                  exists=lambda p: True)
        built, registrar = entry.runtime_for(c)
        assert built.kind == "macos-appliance"
        assert built._fs.__class__.__name__ == "GuestFs"
        assert registrar._fs.__class__.__name__ == "GuestFs"

    def test_the_password_is_read_from_its_file_not_from_the_config(
            self, tmp_path):
        secret = tmp_path / "guest.pass"
        secret.write_text("alpine\n", encoding="utf-8")
        c = parse(dict(GOOD, runtime="macos-appliance",
                       guest=dict(self.GUEST, password_file=str(secret))),
                  exists=lambda p: True)
        assert "alpine" not in json.dumps(c.guest), \
            "the configuration holds the path, never the credential"
        built, _ = entry.runtime_for(c)
        assert built._run._password == "alpine"
