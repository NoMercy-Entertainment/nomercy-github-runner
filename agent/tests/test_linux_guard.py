import hashlib
import io
import subprocess
import tarfile

import pytest

from agent.runtimes import linux_guard

IMAGE = "sha256:" + "a" * 64
BASELINE = "b" * 64
FILES = ("/runner/run", "/runner/runner_guard.py")


def test_only_guard_content_changes_are_reported_and_baseline_is_cached():
    calls = []
    contents = {path: b"original" for path in FILES}

    def run(args, **kwargs):
        calls.append(args)
        return True, BASELINE if args[0] == "create" else BASELINE, ""

    def read(container, path):
        content = b"original" if container == BASELINE else contents[path]
        return hashlib.sha256(content).hexdigest()

    check = linux_guard.ImageGuardFiles(run, read)
    assert check("managed", IMAGE, FILES) == []
    contents[FILES[1]] = b"changed"
    assert check("managed", IMAGE, FILES) == [FILES[1]]
    assert [c[0] for c in calls] == ["create", "rm"]
    assert calls[1] == ["rm", BASELINE]
    assert "start" not in calls[0] and "--read-only" in calls[0]


@pytest.mark.parametrize("failure", [RuntimeError("unreadable"), subprocess.TimeoutExpired("cp", 30)])
def test_unreadable_baselines_are_removed_and_never_report_protection(failure):
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return True, BASELINE, ""

    def read(*args):
        raise failure

    checker = linux_guard.ImageGuardFiles(run, read)
    assert checker("managed", IMAGE, FILES) is None
    assert calls[-1] == ["rm", BASELINE]
    assert not checker.baselines


def test_mutable_image_names_are_refused_without_creating_units():
    checker = linux_guard.ImageGuardFiles(lambda *a, **k: pytest.fail("must not create"))
    assert checker("managed", "image:latest", FILES) is None


@pytest.mark.parametrize("kind,name,size,valid", [
    (tarfile.REGTYPE, "run", 4, True),
    (tarfile.REGTYPE, "./run", 4, True),
    (tarfile.SYMTYPE, "run", 0, False),
    (tarfile.LNKTYPE, "run", 0, False),
    (tarfile.REGTYPE, "../run", 4, False),
    (tarfile.REGTYPE, "other", 4, False),
    (tarfile.REGTYPE, "run", linux_guard.MAX_FILE_BYTES + 1, False),
])
def test_guard_archives_never_follow_links_or_read_unbounded_files(monkeypatch, kind, name, size, valid):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        member = tarfile.TarInfo(name)
        member.type, member.size = kind, size
        if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            member.linkname = "/other"
        archive.addfile(member, io.BytesIO(b"x" * size) if size else None)
    stream.seek(0)

    class Process:
        stdout = stream
        def poll(self): return 0
        def wait(self, **kwargs): return 0
        def kill(self): pytest.fail("finished copy must not be killed")

    monkeypatch.setattr(linux_guard.subprocess, "Popen", lambda *a, **k: Process())
    if valid:
        assert linux_guard.archive_digest("managed", "/runner/run") == hashlib.sha256(b"x" * size).hexdigest()
    else:
        with pytest.raises(ValueError):
            linux_guard.archive_digest("managed", "/runner/run")
