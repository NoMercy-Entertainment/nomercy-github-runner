import ntpath
import os

import pytest

from agent.windows_workspace import alias_path, ensure_alias, remove_alias

RID = "98ce7e34-1498-4825-acd5-4d58af67816f"


def test_short_path_preserves_the_full_runner_identity():
    assert alias_path(r"D:\w", RID) == r"D:\w\98ce7e3414984825acd54d58af67816f"
    with pytest.raises(ValueError):
        alias_path(r"relative\w", RID)
    with pytest.raises(ValueError):
        alias_path(r"D:\w", "..\\other")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction paths")
def test_an_existing_directory_cannot_be_replaced_with_a_workspace_alias(tmp_path):
    root, target = tmp_path / "aliases", tmp_path / "work"
    target.mkdir()
    alias = root / RID.replace("-", "")
    alias.mkdir(parents=True)
    (alias / "retain.txt").write_text("owned elsewhere")
    with pytest.raises(RuntimeError, match="different directory"):
        ensure_alias(str(root), RID, str(target), lambda *a, **k: (True, "", ""),
                     "icacls", "powershell")
    assert (alias / "retain.txt").read_text() == "owned elsewhere"
    with pytest.raises(RuntimeError, match="ownership changed"):
        remove_alias(str(root), RID, str(target))


@pytest.mark.parametrize("architecture,least", [("arm64", 300), ("amd64", 30)])
def test_the_alias_waits_as_long_as_the_machine_needs_to_start_a_shell(tmp_path, monkeypatch, architecture, least):
    """An emulated ARM64 guest takes minutes to start PowerShell, so a 30 s
    bound timed the alias out and failed github-windows-arm64-1's recreate
    with "create_unit: timed out after 30s" (2026-10-08)."""
    root, target = tmp_path / "aliases", tmp_path / "work"
    target.mkdir()
    # The bound is what is under test, not the drive-letter rule, which
    # refuses the POSIX tmp_path the Linux CI runner hands out.
    from agent import windows_workspace
    monkeypatch.setattr(windows_workspace, "alias_path", lambda base, rid: str(root / "alias"))
    seen = []

    def run(args, timeout):
        seen.append(timeout)
        return True, "", ""

    with pytest.raises(RuntimeError):      # the fake makes no junction
        ensure_alias(str(root), RID, str(target), run, "icacls", "powershell",
                     architecture=architecture)
    assert len(seen) == 2 and min(seen) >= least
