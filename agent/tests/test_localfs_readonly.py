"""A runner's directory is removed even when a job left read-only files in it.

git marks the files under .git/objects read-only, and on Windows a read-only
file cannot be deleted. Removing github-windows-arm64-1 for a recreate failed
on exactly that on 2026-10-08 ("storage left behind: ... Access is denied"),
leaving the runner in `removing` with its unit gone.
"""
import os
import stat

from agent.runtimes.localfs import LocalFs


def read_only_tree(root):
    objects = root / "work" / "repo" / ".git" / "objects" / "0c"
    objects.mkdir(parents=True)
    blob = objects / "a05f1ec67a7181bfd22dec750b4d7f1e4c8173"
    blob.write_bytes(b"blob")
    os.chmod(blob, stat.S_IREAD)
    os.chmod(objects, stat.S_IREAD | stat.S_IEXEC)
    return blob


def test_rmtree_removes_read_only_files(tmp_path):
    root = tmp_path / "runner"
    read_only_tree(root)
    LocalFs().rmtree(str(root))
    assert not root.exists()


def test_clear_dir_removes_read_only_files_and_keeps_the_dir(tmp_path):
    root = tmp_path / "runner"
    read_only_tree(root)
    LocalFs().clear_dir(str(root))
    assert root.is_dir() and not any(root.iterdir())


def test_rmtree_of_a_missing_path_is_a_no_op(tmp_path):
    LocalFs().rmtree(str(tmp_path / "absent"))
