"""What a runtime reports of a runner's origin check (agent/origin_guard.py)."""
import json

from agent import origin_guard as og


def record(result="allowed", version=1, at="2026-10-09T10:00:00Z"):
    return json.dumps({"version": version, "result": result, "at": at})


class TestTheLastRunRecord:
    def test_a_record_the_hook_wrote(self):
        got = og.last_result(record())
        assert got == {"result": "allowed", "at": "2026-10-09T10:00:00Z", "version": 1}

    def test_anything_else_is_no_record(self):
        for text in ("", "{", "[]", record(result="maybe"), json.dumps({"result": "allowed"})):
            assert og.last_result(text) is None, text

    def test_a_record_from_another_version_proves_nothing(self):
        """The log directory outlives a recreate: a record the old hook left
        says nothing about the new one."""
        assert og.report(2, last=og.last_result(record(version=1)))["last"] is None
        assert og.report(1, last=og.last_result(record(version=1)))["last"] == {
            "result": "allowed", "at": "2026-10-09T10:00:00Z"}


class TestTheReport:
    def test_missing_changed_and_old_hooks_carry_no_version(self):
        assert og.report(1, missing=True)["version"] == 0
        assert og.report(1, changed=["a.py"])["version"] == 0
        assert og.report(0)["note"] == "hooks from before the check"

    def test_versions_are_read_from_either_spelling(self):
        assert og.guard_version("x\nGUARD_VERSION = 3\n", "GUARD_VERSION = ") == 3
        assert og.guard_version("const GUARD_VERSION = 4;\n", "const GUARD_VERSION = ") == 4
        assert og.guard_version("const GUARD_VERSION = 4x;\n", "const GUARD_VERSION = ") == 0
