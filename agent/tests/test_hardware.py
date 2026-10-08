"""What the machine an agent runs on has: logical CPUs and physical memory.

The controller bounds every CPU and memory limit by this (GitHub #5), so it
is measured on the machine rather than taken from configuration, and a
reading that fails is None - unknown - never a guess and never an error that
stops the agent from declaring everything else.
"""
import ctypes
import os

from agent import hardware

GIB = 1024**3


class TestLinuxMemory:
    def test_memtotal_in_bytes(self, tmp_path):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       82440408 kB\nMemFree:  1 kB\n"
                           "SwapTotal:      0 kB\n", encoding="ascii")
        assert hardware.linux_memory(str(meminfo)) == 82440408 * 1024

    def test_unreadable_is_unknown(self, tmp_path):
        assert hardware.linux_memory(str(tmp_path / "missing")) is None
        garbled = tmp_path / "garbled"
        garbled.write_text("MemTotal: lots\n", encoding="ascii")
        assert hardware.linux_memory(str(garbled)) is None


class Kernel32:
    """GlobalMemoryStatusEx as Windows answers it: fills the structure it is
    handed a pointer to, and returns nonzero."""

    def __init__(self, total, ok=True):
        self.total, self.ok, self.length = total, ok, None

    def GlobalMemoryStatusEx(self, pointer):
        status = pointer._obj
        self.length = status.dwLength
        status.ullTotalPhys = self.total
        return 1 if self.ok else 0


class TestWindowsMemory:
    def test_total_physical_memory(self):
        kernel32 = Kernel32(16 * GIB)
        assert hardware.windows_memory(kernel32) == 16 * GIB
        assert kernel32.length == ctypes.sizeof(hardware.MEMORYSTATUSEX), \
            "Windows refuses the call unless dwLength is the structure's size"

    def test_a_failed_call_is_unknown(self):
        assert hardware.windows_memory(Kernel32(16 * GIB, ok=False)) is None

    def test_where_there_is_no_kernel32_it_is_unknown(self, monkeypatch):
        monkeypatch.setattr(hardware, "_kernel32", lambda: None)
        assert hardware.windows_memory() is None


class TestFacts:
    def test_both_keys_always(self, tmp_path):
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal: 1024 kB\n", encoding="ascii")
        facts = hardware.facts("linux", meminfo=str(meminfo))
        assert facts == {"logical_cpus": os.cpu_count(), "memory_bytes": 1024 * 1024}

    def test_windows_reads_kernel32_not_proc(self):
        facts = hardware.facts("win32", kernel32=Kernel32(8 * GIB))
        assert facts == {"logical_cpus": os.cpu_count(), "memory_bytes": 8 * GIB}

    def test_nothing_raises(self, monkeypatch):
        monkeypatch.setattr(os, "cpu_count", lambda: None)
        monkeypatch.setattr(hardware, "_kernel32", lambda: None)
        assert hardware.facts("win32") == {"logical_cpus": None, "memory_bytes": None}
