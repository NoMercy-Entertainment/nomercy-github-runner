"""The fleets: seeding them, and what may be asked of them.

Two properties carry the weight. Seeding must be safe to run on every start,
because it will be - so it may create what is missing and refresh what is
derived, and must never touch what an operator set. And a cell nobody can build
must refuse capacity at the moment it is asked for, not at the moment a runner
half exists.
"""
import pytest

import providers
from store import schema
from store.fleets import (CELLS, FleetStore, FleetUnavailable, UnknownFleet,
                          fleet_id)


@pytest.fixture
def fleets(tmp_path):
    path = str(tmp_path / "control.db")
    schema.init(path)
    return FleetStore(path)


#: Names a self-built Forgejo agent, which is what makes those cells buildable.
BUILT = {
    "FORGEJO_RUNNER_ARTIFACT_WINDOWS": "forgejo-runner-windows-amd64.exe",
    "FORGEJO_RUNNER_ARTIFACT_MACOS": "forgejo-runner-darwin-arm64",
}


class TestSeeding:
    def test_there_are_eight(self, fleets):
        fleets.seed()
        assert len(fleets.list()) == 8

    def test_seeded_fleets_are_the_declared_cells(self, fleets):
        fleets.seed()
        seen = {(f["provider"], f["platform"], f["architecture"])
                for f in fleets.list()}
        assert seen == set(CELLS)

    def test_seeding_twice_changes_nothing(self, fleets):
        """It runs on every start, so this is the ordinary path."""
        fleets.seed()
        before = fleets.list()
        fleets.seed()
        assert fleets.list() == before

    def test_seeding_does_not_reset_an_operators_capacity(self, fleets):
        """The failure this guards: a redeploy silently zeroing a fleet."""
        fleets.seed()
        fid = fleet_id("github", "linux", "x64")
        fleets.set_capacity(fid, 8)
        fleets.seed()
        assert fleets.get(fid)["desired_capacity"] == 8

    def test_every_fleet_starts_empty(self, fleets):
        """Seeding states what is possible, never what should be running."""
        fleets.seed()
        assert all(f["desired_capacity"] == 0 for f in fleets.list())


class TestAvailabilityComesFromTheProvider:
    def test_windows_arm_forgejo_requires_its_own_binary(self, fleets):
        fleets.seed(BUILT)
        arm = fleets.get("forgejo-windows-arm64")
        assert arm["available"] is False
        assert "FORGEJO_RUNNER_ARTIFACT_WINDOWS_ARM64" in arm["unavailable_reason"]
        fleets.seed(dict(BUILT, FORGEJO_RUNNER_ARTIFACT_WINDOWS_ARM64="forgejo-arm.exe"))
        assert fleets.get("forgejo-windows-arm64")["available"] is True

    def test_the_github_cells_are_available(self, fleets):
        fleets.seed()
        for platform in providers.PLATFORMS:
            fid = fleet_id("github", platform, "x64")
            assert fleets.get(fid)["available"] is True, fid

    def test_forgejo_linux_is_available(self, fleets):
        fleets.seed()
        assert fleets.get(fleet_id("forgejo", "linux", "x64"))["available"]

    def test_the_forgejo_cells_with_no_binary_are_not(self, fleets):
        """Forgejo publishes Linux assets only."""
        fleets.seed()
        for platform in ("windows", "macos"):
            fleet = fleets.get(fleet_id("forgejo", platform, "x64"))
            assert fleet["available"] is False, platform

    def test_an_unavailable_fleet_says_why(self, fleets):
        """A bare false makes an unavailable fleet and a broken one look the
        same, and only one of those is something an operator can fix."""
        fleets.seed()
        fleet = fleets.get(fleet_id("forgejo", "windows", "x64"))
        assert "publishes no windows runner binary" in \
            fleet["unavailable_reason"]
        assert "FORGEJO_RUNNER_ARTIFACT_WINDOWS" in fleet["unavailable_reason"]

    def test_naming_a_self_built_artefact_makes_the_cell_available(
            self, fleets):
        fleets.seed(BUILT)
        fleet = fleets.get(fleet_id("forgejo", "windows", "x64"))
        assert fleet["available"] is True
        assert fleet["unavailable_reason"] is None

    def test_reseeding_refreshes_availability(self, fleets):
        """Availability is derived, so it must follow the world. Someone who
        builds the artefact should not have to recreate the database."""
        fleets.seed()
        assert not fleets.get(fleet_id("forgejo", "macos", "x64"))["available"]
        fleets.seed(BUILT)
        assert fleets.get(fleet_id("forgejo", "macos", "x64"))["available"]

    def test_a_fleet_records_what_its_runners_are_built_from(self, fleets):
        fleets.seed()
        fleet = fleets.get(fleet_id("forgejo", "linux", "x64"))
        assert fleet["template"]

    def test_an_unbuildable_fleet_has_no_template(self, fleets):
        """Rather than a guess, which would be found wrong at registration
        time on a runner that already exists."""
        fleets.seed()
        assert fleets.get(
            fleet_id("forgejo", "windows", "x64"))["template"] is None


class TestCapacity:
    def test_it_is_per_fleet(self, fleets):
        fleets.seed()
        fleets.set_capacity(fleet_id("github", "linux", "x64"), 8)
        assert fleets.get(
            fleet_id("github", "linux", "x64"))["desired_capacity"] == 8
        assert fleets.get(
            fleet_id("github", "macos", "x64"))["desired_capacity"] == 0

    def test_setting_it_returns_an_operation_to_follow(self, fleets):
        """Capacity is an intention the controller works through, so the
        caller gets something it can watch rather than a boolean."""
        fleets.seed()
        operation_id = fleets.set_capacity(
            fleet_id("github", "linux", "x64"), 4)
        assert len(operation_id) == 36

    def test_the_operation_records_what_was_asked_and_by_whom(self, fleets):
        fleets.seed()
        fid = fleet_id("github", "linux", "x64")
        operation_id = fleets.set_capacity(fid, 4, requested_by="sub-admin")
        with schema.connect(fleets.path) as c:
            row = c.execute("SELECT * FROM operations WHERE operation_id = ?",
                            (operation_id,)).fetchone()
        assert row["verb"] == "set_capacity"
        assert row["fleet_id"] == fid
        assert row["requested_by"] == "sub-admin"
        assert row["state"] == "pending"

    def test_an_unsupported_fleet_cannot_be_given_capacity(self, fleets):
        """Refused where it is asked, not on a runner that half exists."""
        fleets.seed()
        with pytest.raises(FleetUnavailable) as caught:
            fleets.set_capacity(fleet_id("forgejo", "windows", "x64"), 1)
        assert "FORGEJO_RUNNER_ARTIFACT_WINDOWS" in str(caught.value)

    def test_a_refused_request_opens_no_operation(self, fleets):
        fleets.seed()
        with pytest.raises(FleetUnavailable):
            fleets.set_capacity(fleet_id("forgejo", "macos", "x64"), 1)
        with schema.connect(fleets.path) as c:
            assert c.execute(
                "SELECT count(*) FROM operations").fetchone()[0] == 0

    def test_it_becomes_settable_once_the_artefact_exists(self, fleets):
        fleets.seed(BUILT)
        assert fleets.set_capacity(fleet_id("forgejo", "macos", "x64"), 2)

    def test_zero_is_a_legitimate_capacity(self, fleets):
        """Scaling a fleet to nothing is how it is retired."""
        fleets.seed()
        fid = fleet_id("github", "linux", "x64")
        fleets.set_capacity(fid, 4)
        fleets.set_capacity(fid, 0)
        assert fleets.get(fid)["desired_capacity"] == 0

    def test_a_negative_capacity_is_refused(self, fleets):
        fleets.seed()
        with pytest.raises(ValueError):
            fleets.set_capacity(fleet_id("github", "linux", "x64"), -1)

    def test_an_unknown_fleet_is_refused(self, fleets):
        fleets.seed()
        with pytest.raises(UnknownFleet):
            fleets.set_capacity("github-plan9-x64", 1)


class TestAddingASeventhCellNeedsNoCode:
    def test_a_fleet_is_a_row(self, fleets):
        """FR-15's actual claim, checked. A cell the matrix does not list can
        be inserted and read back like any other."""
        fleets.seed()
        with schema.connect(fleets.path) as c:
            c.execute(
                "INSERT INTO fleets (fleet_id, provider, platform,"
                " architecture, available) VALUES"
                " ('github-linux-arm64', 'github', 'linux', 'arm64', 1)")
        assert len(fleets.list()) == 9
        fleets.set_capacity("github-linux-arm64", 2)
        assert fleets.get("github-linux-arm64")["desired_capacity"] == 2

    def test_a_cell_cannot_be_registered_twice(self, fleets):
        """UNIQUE on the coordinate is what makes the fleet id canonical."""
        import sqlite3
        fleets.seed()
        with schema.connect(fleets.path) as c:
            with pytest.raises(sqlite3.IntegrityError):
                c.execute(
                    "INSERT INTO fleets (fleet_id, provider, platform,"
                    " architecture) VALUES"
                    " ('another-name', 'github', 'linux', 'x64')")


GIB = 1024**3


def _host(fleets, host_id, worker_kind="hyperv-linux", healthy=True, **caps):
    from control.inventory import Inventory
    workers = Inventory(fleets.path)
    workers.register_worker(host_id, worker_kind, capabilities=caps)
    if healthy:
        workers.heartbeat(host_id)


class TestLimitsAgainstHardware:
    """A fleet's CPU and memory defaults are bounded by the hardware its
    workers report. Accepted when one host of the cell can hold them, refused
    with the host and its maximum when none can, and accepted - flagged
    elsewhere - when no host has said what it has."""

    def test_a_limit_one_host_can_hold_is_saved(self, fleets):
        fleets.seed()
        _host(fleets, "rnr-linux-1", kind="linux-container", host_cores=56,
              memory_total_bytes=84418977792)
        saved = fleets.set_defaults("github-linux-x64",
                                    {"cpu_limit": 16, "memory_limit": 32 * GIB})
        assert saved["cpu_limit"] == "16.0" and saved["memory_limit"] == 32 * GIB

    def test_more_cores_than_any_host_is_refused_naming_it(self, fleets):
        fleets.seed()
        _host(fleets, "rnr-linux-1", kind="linux-container", host_cores=56,
              memory_total_bytes=84418977792)
        with pytest.raises(ValueError) as refused:
            fleets.set_defaults("github-linux-x64", {"cpu_limit": 64})
        assert "rnr-linux-1" in str(refused.value) and "56" in str(refused.value)
        assert fleets.get("github-linux-x64")["cpu_limit"] is None

    def test_more_memory_than_any_host_is_refused_naming_it(self, fleets):
        fleets.seed()
        _host(fleets, "rnr-linux-1", kind="linux-container", host_cores=56,
              memory_total_bytes=84418977792)
        with pytest.raises(ValueError) as refused:
            fleets.set_defaults("github-linux-x64", {"memory_limit": 96 * GIB})
        assert "rnr-linux-1" in str(refused.value)
        assert "78.6 GiB" in str(refused.value)

    def test_a_degraded_host_still_counts(self, fleets):
        """Hardware does not change because a heartbeat is late."""
        fleets.seed()
        _host(fleets, "rnr-linux-1", healthy=False, kind="linux-container",
              host_cores=56)
        assert fleets.set_defaults("github-linux-x64", {"cpu_limit": 16})["cpu_limit"] == "16.0"
        with pytest.raises(ValueError):
            fleets.set_defaults("github-linux-x64", {"cpu_limit": 64})

    def test_another_cells_host_does_not_count(self, fleets):
        fleets.seed()
        _host(fleets, "rnr-linux-1", kind="linux-container", host_cores=56)
        _host(fleets, "rnr-windows-1", "hyperv-windows", kind="windows-process",
              host_cores=16)
        _host(fleets, "windows-arm64-1", "hyperv-windows", kind="windows-process",
              host_cores=8, architecture="arm64")
        assert fleets.set_defaults("github-windows-x64", {"cpu_limit": 16})
        with pytest.raises(ValueError) as refused:
            fleets.set_defaults("github-windows-arm64", {"cpu_limit": 16})
        assert "windows-arm64-1" in str(refused.value)

    def test_unknown_hardware_is_accepted(self, fleets):
        fleets.seed()
        _host(fleets, "rnr-windows-1", "hyperv-windows", kind="windows-process",
              host_cores=16)
        saved = fleets.set_defaults("github-windows-x64", {"memory_limit": 512 * GIB})
        assert saved["memory_limit"] == 512 * GIB

    def test_other_settings_are_saved_whatever_the_hardware_now_is(self, fleets):
        """A host that shrank after a limit was saved must not lock the
        fleet's labels: only a change to a limit is checked."""
        fleets.seed()
        _host(fleets, "rnr-linux-1", kind="linux-container", host_cores=56)
        fleets.set_defaults("github-linux-x64", {"cpu_limit": 32})
        _host(fleets, "rnr-linux-1", kind="linux-container", host_cores=16)
        assert fleets.set_defaults("github-linux-x64", {"labels": ["build"]})["labels"] == ["build"]


class TestLimitNormalisation:
    def test_cpu_is_a_positive_number(self):
        from store import limits
        assert limits.normalize_cpu("16", "linux") == "16.0"
        assert limits.normalize_cpu(2.5, "windows") == "2.5"
        assert limits.normalize_cpu("4.0", "macos") == "4"
        assert limits.normalize_cpu(None, "linux") is None
        for bad in (0, -1, True, "x", float("inf"), "0-15"):
            with pytest.raises(ValueError):
                limits.normalize_cpu(bad, "linux")
        for bad in ("1.5", 65):
            with pytest.raises(ValueError):
                limits.normalize_cpu(bad, "macos")

    def test_memory_is_positive_bytes(self):
        from store import limits
        assert limits.normalize_memory(8 * GIB, "linux") == 8 * GIB
        assert limits.normalize_memory(None, "linux") is None
        for bad in (0, -1, True, "8g", 1.5, 2**63):
            with pytest.raises(ValueError):
                limits.normalize_memory(bad, "linux")
        for bad in (2 * GIB, 129 * GIB, 4 * GIB + 1):
            with pytest.raises(ValueError):
                limits.normalize_memory(bad, "macos")
