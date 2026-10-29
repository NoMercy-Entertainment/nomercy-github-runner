import importlib.util
import json
from pathlib import Path

import pytest

module_path = Path(__file__).resolve().parents[2] / "infra/windows/guest/set_runner_update_policy.py"
spec = importlib.util.spec_from_file_location("runner_update_policy", module_path)
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class Registry:
    HKEY_LOCAL_MACHINE, KEY_SET_VALUE, REG_DWORD = "local", 2, 4

    def __init__(self, model="Microsoft Corporation Virtual Machine"):
        self.values = {policy.AU: {"NoAutoRebootWithLoggedOnUsers": (1, 4), "Unrelated": ("keep", 1)},
                       policy.WU: {}, r"HARDWARE\DESCRIPTION\System\BIOS": {"SystemProductName": (model, 1)}}
        self.writes = []

    class Key:
        def __init__(self, path): self.path = path
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def OpenKey(self, hive, path):
        assert hive == self.HKEY_LOCAL_MACHINE
        if path not in self.values: raise FileNotFoundError()
        return self.Key(path)

    def EnumValue(self, key, index):
        items = list(self.values[key.path].items())
        if index >= len(items):
            error = OSError()
            error.winerror = 259
            raise error
        name, (value, kind) = items[index]
        return name, value, kind

    def CreateKeyEx(self, hive, path, reserved, access):
        assert path == policy.AU and access == self.KEY_SET_VALUE
        return self.Key(path)

    def SetValueEx(self, key, name, reserved, kind, value):
        assert key.path == policy.AU
        self.writes.append(name)
        self.values[key.path][name] = (value, kind)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "agent.json").write_text(json.dumps({"host_id": "rnr-windows-1"}))
    return tmp_path


def test_read_only_policy_check_has_no_mutations(root):
    registry = Registry()
    result = policy.apply_policy(registry, root, "rnr-windows-1", "GUEST")
    assert result["policy"]["NoAutoUpdate"] is None
    assert not registry.writes and not (root / "update-policy-before-manual-maintenance.json").exists()


@pytest.mark.parametrize("computer,expected,model", [("BEAST-UNIT", "rnr-windows-1", "Virtual Machine"),
    ("EAGLE", "rnr-windows-1", "Physical PC"), ("GUEST", "other-worker", "Virtual Machine")])
def test_unselected_or_physical_hosts_are_never_changed(root, computer, expected, model):
    registry = Registry(model)
    with pytest.raises(RuntimeError):
        policy.apply_policy(registry, root, expected, computer, True)
    assert not registry.writes and not (root / "update-policy-before-manual-maintenance.json").exists()


def test_a_guest_enrolled_under_another_identity_is_never_changed(tmp_path):
    (tmp_path / "agent.json").write_text(json.dumps({"host_id": "some-other-host"}))
    registry = Registry()
    with pytest.raises(RuntimeError, match="identity"):
        policy.apply_policy(registry, tmp_path, "rnr-windows-1", "GUEST", True)
    assert not registry.writes and not (tmp_path / "update-policy-before-manual-maintenance.json").exists()


def test_existing_forced_deadline_stops_before_mutation(root):
    registry = Registry()
    registry.values[policy.WU]["ConfigureDeadlineForQualityUpdates"] = (2, 4)
    with pytest.raises(RuntimeError, match="deadline"):
        policy.apply_policy(registry, root, "rnr-windows-1", "GUEST", True)
    assert not registry.writes


def test_manual_maintenance_is_verified_and_keeps_original_backup(root):
    registry = Registry()
    first = policy.apply_policy(registry, root, "rnr-windows-1", "GUEST", True)
    backup = Path(first["backup"]).read_bytes()
    assert first["policy"]["NoAutoUpdate"] == 1
    assert registry.values[policy.AU]["Unrelated"] == ("keep", 1)
    policy.apply_policy(registry, root, "rnr-windows-1", "GUEST", True)
    assert Path(first["backup"]).read_bytes() == backup
    assert "NoAutoUpdate" not in json.loads(backup)["values"]
