"""Every way this platform is still tied to WSL, as a test that fails.

Section 2.5 of the design lists nine couplings, measured rather than guessed.
This file turns that list into something a machine checks. Each test asserts
the coupling is GONE, which is false today, so each is marked xfail. The
migration flips them one at a time, and T-1708 removes the markers.

Why assert the future rather than the present. A test that pinned today's
`/mnt/d` would pass forever and would have to be deleted by the very change it
was meant to track - it would guard the coupling instead of measuring it.
Written this way the file is a progress bar: the number of xfails left is the
number of couplings left, and "WSL is gone" stops being a claim anyone has to
take on trust.

The markers are strict. When a coupling disappears the test starts passing,
and a strict xfail turns that into a loud failure naming the row - which is
the reminder to unmark it. Silent success is the one outcome that would let
this file drift out of step with reality, which is the failure it exists to
prevent. A red suite here means good news and a one-line edit.

Everything below reads files. Nothing starts, stops or inspects a runner.
"""
import os

import pytest

REASON = "still WSL-coupled; unmarked by T-1708"
wsl = pytest.mark.xfail(reason=REASON, strict=True)

DASH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(DASH)
SPEC = os.path.join(
    ROOT, "docs", "superpowers", "specs",
    "2026-09-17-uniform-hyperv-runner-platform-design.md")


def read(*parts):
    """A repo file's text, or "" when it no longer exists.

    Deleting the file IS the fix for several of these rows, so a missing file
    must read as "the coupling is gone", not as an error.
    """
    path = os.path.join(ROOT, *parts)
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def scripts_text():
    """Every PowerShell script, concatenated.

    Read as one body because a coupling that merely moves between scripts has
    not been removed, and a per-file assertion would call that progress.
    """
    d = os.path.join(ROOT, "scripts")
    if not os.path.isdir(d):
        return ""
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".ps1"):
            out.append(read("scripts", name))
    return "\n".join(out)


# --------------------------------------------------------------- the nine rows

@wsl
def test_the_repo_is_not_mounted_through_a_wsl_path():
    """Row 1. `/mnt/d` only exists inside the distro.

    The daemon resolves bind mounts, so this path is what decides whether a
    runner sees the repository at all. A Hyper-V worker has no /mnt/d, and a
    host path has to be passed in rather than defaulted to.
    """
    compose = read("docker-compose.runners.yml")
    ops = read("dashboard", "docker_ops.py")
    assert "/mnt/d" not in compose, (
        "docker-compose.runners.yml still mounts the repo from a WSL path")
    assert "/mnt/d" not in ops, (
        "REPO_HOST_PATH still defaults to a WSL path; a worker that is not "
        "the distro would silently bind an empty directory")


@wsl
def test_external_telemetry_is_not_reached_through_the_wsl_nat_gateway():
    """Row 2. The exporter is addressed at the WSL virtual switch gateway.

    That address is assigned by WSL, changes when the distro restarts, and does
    not exist once the dashboard is not inside it.

    Skipped rather than passed where there is no .env: a checkout with nothing
    configured says nothing about whether this host is still coupled, and an
    empty answer must not read as progress.
    """
    env = read(".env")
    if not env:
        pytest.skip("no .env on this host; nothing to measure")
    line = next((ln for ln in env.splitlines()
                 if ln.startswith("EXTERNAL_EXPORTER_URL=")), "")
    host = line.partition("=")[2].strip()
    assert not host.startswith("http://172.2"), (
        "the exporter is addressed on the WSL NAT range; it needs an address "
        "that survives the distro")


@wsl
def test_fleet_disk_is_not_measured_through_the_dashboards_own_volume():
    """Row 3. One statvfs of /data stands in for the whole fleet's disk.

    True only while a single VHDX backs every runner. Once workers are separate
    VMs each has its own disk, and one number measured inside the dashboard is
    not a fleet figure - it is one worker's, reported as everyone's.
    """
    ops = read("dashboard", "docker_ops.py")
    assert "statvfs" not in ops, (
        "disk still comes from a local statvfs; it must come from each "
        "worker's telemetry")


@wsl
def test_the_ui_does_not_name_a_windows_path():
    """Row 4. The disk card renders a literal VHDX path.

    Hard-coded, not measured, so it keeps looking authoritative after it stops
    being true - and a Hyper-V worker's disk is not a VHDX at that path.
    """
    page = read("dashboard", "templates", "index.html")
    assert "ext4.vhdx" not in page, (
        "the disk card still names a VHDX; it must show where the measurement "
        "actually came from")
    assert "D:\\Docker" not in page, (
        "the page still hard-codes a Windows path")


def test_there_is_no_keepalive_against_distro_idle_shutdown():
    """Row 5. WSL shuts an idle distro down, taking the fleet with it.

    The keepalive exists only because of that behaviour. A Hyper-V VM does not
    stop itself for being quiet, so these scripts have nothing left to do.

    Unmarked: T-6 (2026-09-22) deleted both scripts.
    """
    for script in ("keepalive-distro.ps1", "install-keepalive-task.ps1"):
        assert not os.path.exists(os.path.join(ROOT, "scripts", script)), (
            f"{script} still exists; it keeps an idle WSL distro from shutting "
            f"down and has no counterpart on a VM")


def test_lan_access_does_not_depend_on_a_rewritten_portproxy():
    """Row 6. The dashboard is published through `netsh portproxy`.

    The rule has to be rewritten every time WSL hands out a new address, which
    is why the keepalive loop rewrites it. Two stale rules already point at an
    address the macOS VM no longer has. A VM with a stable address on the LAN
    needs no hop at all.

    Unmarked: T-6 (2026-09-22) deleted the scripts that rewrote the rule
    (keepalive-distro.ps1, publish-dashboard-lan.ps1); the live rule now
    points at a stable Hyper-V worker address and nothing in the repo
    rewrites it.
    """
    text = scripts_text()
    assert "portproxy" not in text, (
        "LAN publication still goes through a rewritten portproxy hop")


@wsl
def test_there_is_no_clock_workaround():
    """Row 7. All distros share one kernel clock, and it drifts.

    The dashboard tolerates skewed session cookies and provisioning masks the
    time daemon, because a second disciplinarian in the distro fights the
    host. A VM keeps its own clock and can simply run one.
    """
    app = read("dashboard", "app.py")
    assert "ClockTolerantSessions" not in app, (
        "sessions still tolerate a skewed clock")
    assert "systemd-timesyncd" not in scripts_text(), (
        "provisioning still masks the time daemon")


def test_there_is_no_dns_workaround():
    """Row 8. Public resolvers are pinned because the WSL DNS proxy died.

    It took the whole fleet offline with UnknownHostException everywhere. The
    pin treats a WSL component's failure, and hard-coded resolvers are a
    liability anywhere else.

    Unmarked: T-6 (2026-09-22) deleted provision-distro.ps1, the only place
    that pinned resolvers.
    """
    assert "resolv.conf" not in scripts_text(), (
        "provisioning still pins resolvers around the WSL DNS proxy")


def test_there_is_no_memory_reclaim_knob():
    """Row 9. `vm.compaction_proactiveness` is raised from WSL's default of 0.

    A tuning for the shared utility VM, where a runner's page cache grew until
    it starved everything else on the same kernel. A worker that owns its
    memory does not need it.

    Unmarked: T-6 (2026-09-22) deleted provision-distro.ps1, the only place
    that tuned this knob.
    """
    assert "compaction_proactiveness" not in scripts_text(), (
        "provisioning still tunes memory reclaim for the shared utility VM")


# ------------------------------------------------------------------ the tally

def coupling_rows():
    """The rows of spec section 2.5, parsed from the spec itself.

    Parsed rather than copied so the two cannot drift: a row added to the
    design without a test here fails the count below.
    """
    if not os.path.exists(SPEC):
        return []
    with open(SPEC, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    rows, inside = [], False
    for line in lines:
        if line.startswith("### 2.5"):
            inside = True
            continue
        if inside and line.startswith("### "):
            break
        if inside and line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) >= 2 and cells[0] and not set(cells[0]) <= {"-", " "}:
                if cells[0] != "Coupling":
                    rows.append(cells[0])
    return rows


class TestEveryRowIsCovered:
    """The task's own definition of done, checked rather than asserted in prose."""

    def test_the_spec_still_lists_nine_couplings(self):
        assert len(coupling_rows()) == 9, coupling_rows()

    def test_there_is_one_test_per_row(self):
        """A row without a test is a coupling nobody is tracking.

        Counted by test function, not by `@wsl` marker. Unmarking a resolved
        row's test is the whole point of this file (see module docstring);
        counting markers instead of tests would misread that progress as a
        row nobody tracks any more.
        """
        with open(os.path.abspath(__file__), encoding="utf-8") as fh:
            source = fh.read()
        start = source.index("the nine rows")
        end = source.index("the tally")
        tests = source[start:end].count("\ndef test_")
        assert tests == len(coupling_rows()), (
            f"{tests} row tests for {len(coupling_rows())} rows of spec "
            f"2.5; every row needs exactly one")

    def test_every_marker_carries_the_agreed_reason(self):
        """`-rx` prints these, so the reason is what an operator reads to find
        out which task removes them."""
        assert REASON == "still WSL-coupled; unmarked by T-1708"
