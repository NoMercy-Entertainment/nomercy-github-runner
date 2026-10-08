"""The volume a runner's directory lives on: how big it is and how full.

A runner with no disk of its own shares one with everything else on the
machine, and that volume - not the runner's own bytes - is what stops it when
it fills. Each runtime reports it beside the runner's own figure, read with
the tool the machine has: `shutil.disk_usage` on the agent's own disk, `df -k`
in a guest or a container. Unknown is None, never zero.
"""
import shutil
from collections import namedtuple

from agent.runtimes.localfs import LocalFs, df_figures

GNU = ("Filesystem     1024-blocks      Used Available Capacity Mounted on\n"
       "/dev/sdd        263174212  12345678 237391212       5% /runner/work\n")
BSD = ("Filesystem     1024-blocks      Used Available Capacity iused"
       "      ifree %iused  Mounted on\n"
       "/dev/disk3s5     267893016 160234560  95012344    63% 1234567"
       " 950123440    0%   /System/Volumes/Data\n")


def test_gnu_df_is_read_in_bytes():
    assert df_figures(GNU) == {"used_bytes": 12345678 * 1024,
                               "total_bytes": 263174212 * 1024}


def test_bsd_df_is_read_in_bytes():
    assert df_figures(BSD) == {"used_bytes": 160234560 * 1024,
                               "total_bytes": 267893016 * 1024}


def test_what_df_did_not_say_is_none():
    for text in ("", None, "Filesystem 1024-blocks Used\n",
                 "Filesystem\nnot numbers here at all\n",
                 "Filesystem\n/dev/x 0 0 0 0% /\n"):
        assert df_figures(text) is None, text


def test_the_local_disk_is_read_from_the_os(tmp_path, monkeypatch):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda path: Usage(500 * 10 ** 9, 200 * 10 ** 9,
                                           300 * 10 ** 9))
    assert LocalFs().disk_usage(str(tmp_path)) == {
        "used_bytes": 200 * 10 ** 9, "total_bytes": 500 * 10 ** 9}


def test_a_local_path_that_is_not_there_is_none(tmp_path):
    assert LocalFs().disk_usage(str(tmp_path / "missing")) is None


def test_the_real_local_disk_has_a_size(tmp_path):
    got = LocalFs().disk_usage(str(tmp_path))
    assert got["total_bytes"] > 0
    assert 0 <= got["used_bytes"] <= got["total_bytes"]
