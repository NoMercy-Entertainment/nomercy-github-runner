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
