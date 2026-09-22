"""Retiring a worker no runner uses (W6b, design 16.8).

`wsl-linux-1` has no runners left, but its row still shows on the page. An
operator needs a way to remove it - and needs to be refused, loudly, if a
runner still names it: deleting a worker out from under a runner that is
still placed there would strand that runner's placement.

"No runner names it" means no spec with this `host_id` that is neither
soft-deleted nor already at the terminal `absent` state (13.4: a worker going
away does not remove its runners, so one still claiming this worker keeps it
until it is itself resolved). A spec that already reached `absent`, or was
soft-deleted, is exempt from that count - but `runner_specs.host_id` is a
foreign key onto `workers`, so its lingering value would otherwise refuse the
very delete this command exists to make.
"""
import sqlite3

import pytest

from control import audit, main
from control import inventory as inv
from store import schema
from store.specs import SpecStore


def make(tmp_path):
    path = str(tmp_path / "c.db")
    schema.init(path)
    return path, inv.Inventory(path), SpecStore(path)


class TestInventoryRetire:
    def test_a_worker_with_no_runners_is_retired(self, tmp_path):
        path, i, specs = make(tmp_path)
        i.register_worker("wsl-linux-1", inv.HYPERV_LINUX,
                          capabilities={"kind": "linux-container"})
        i.retire("wsl-linux-1", specs)
        assert all(w["host_id"] != "wsl-linux-1" for w in i.list())

    def test_a_worker_a_runner_names_is_refused(self, tmp_path):
        path, i, specs = make(tmp_path)
        i.register_worker("w1", inv.HYPERV_LINUX,
                          capabilities={"kind": "linux-container"})
        specs.create(provider="github", platform="linux", host_id="w1",
                     desired_state="running", actual_state="idle")
        with pytest.raises(ValueError, match="names this worker"):
            i.retire("w1", specs)
        assert i.get("w1") is not None, "a refused retire changes nothing"

    def test_the_count_in_the_refusal_is_how_many_runners_name_it(
            self, tmp_path):
        path, i, specs = make(tmp_path)
        i.register_worker("w1", inv.HYPERV_LINUX,
                          capabilities={"kind": "linux-container"})
        for _ in range(2):
            specs.create(provider="github", platform="linux", host_id="w1",
                         desired_state="running", actual_state="idle")
        with pytest.raises(ValueError, match=r"^2 runner\(s\) names this worker"):
            i.retire("w1", specs)

    def test_a_drained_or_stopped_runner_still_names_its_worker(
            self, tmp_path):
        """Only `absent` is terminal. `drained` and `stopped` are ordinary
        states a runner comes back from, and a worker deleted from under one
        would leave it placed nowhere real."""
        path, i, specs = make(tmp_path)
        i.register_worker("w1", inv.HYPERV_LINUX,
                          capabilities={"kind": "linux-container"})
        specs.create(provider="github", platform="linux", host_id="w1",
                     desired_state="drained", actual_state="drained")
        with pytest.raises(ValueError, match="names this worker"):
            i.retire("w1", specs)

    def test_a_runner_that_reached_absent_does_not_block_it(self, tmp_path):
        path, i, specs = make(tmp_path)
        i.register_worker("wsl-linux-1", inv.HYPERV_LINUX,
                          capabilities={"kind": "linux-container"})
        runner_id = specs.create(provider="github", platform="linux",
                                 host_id="wsl-linux-1",
                                 desired_state="absent",
                                 actual_state="absent")
        i.retire("wsl-linux-1", specs)
        assert i.get("wsl-linux-1") is None
        assert specs.get(runner_id) is not None, \
            "the spec is history and stays readable"

    def test_a_soft_deleted_runner_does_not_block_it_either(self, tmp_path):
        path, i, specs = make(tmp_path)
        i.register_worker("w1", inv.HYPERV_LINUX,
                          capabilities={"kind": "linux-container"})
        runner_id = specs.create(provider="github", platform="linux",
                                 host_id="w1", desired_state="running",
                                 actual_state="idle")
        specs.soft_delete(runner_id)
        i.retire("w1", specs)
        assert i.get("w1") is None

    def test_an_absent_runners_lingering_host_id_does_not_refuse_the_delete(
            self, tmp_path):
        """`runner_specs.host_id` is `REFERENCES workers(host_id)`, and
        SQLite enforces it (store/schema.py, `PRAGMA foreign_keys=ON`). A
        row that reached `absent` still carries the worker it last ran on
        (control/reconciler.py's `_move` never clears it) - so retiring that
        worker without also releasing the reference would fail the DELETE
        with an IntegrityError, not the clean refusal this command exists to
        give. This is that failure, caught."""
        path, i, specs = make(tmp_path)
        i.register_worker("w1", inv.HYPERV_LINUX,
                          capabilities={"kind": "linux-container"})
        specs.create(provider="github", platform="linux", host_id="w1",
                     desired_state="absent", actual_state="absent")
        i.retire("w1", specs)          # must not raise sqlite3.IntegrityError
        assert i.get("w1") is None

    def test_an_unknown_host_id_is_refused_not_silently_accepted(
            self, tmp_path):
        """A `DELETE ... WHERE host_id = ?` that matches nothing still
        returns normally - so without this, a typo'd host_id would read as
        a worker successfully retired, exactly like every other Inventory
        method that is refused rather than silently doing nothing."""
        path, i, specs = make(tmp_path)
        with pytest.raises(inv.UnknownWorker):
            i.retire("no-such-worker", specs)

    def test_it_is_one_transaction_the_specs_argument_does_not_drive(
            self, tmp_path):
        """The count that decides the refusal has to come from the same
        transaction as the writes, not from a second connection (`specs`)
        that could be looking at an earlier or later world - so a caller
        that passes `None` instead of a `SpecStore` gets the same answer."""
        path, i, specs = make(tmp_path)
        i.register_worker("w1", inv.HYPERV_LINUX,
                          capabilities={"kind": "linux-container"})
        specs.create(provider="github", platform="linux", host_id="w1",
                     desired_state="running", actual_state="idle")
        with pytest.raises(ValueError, match="names this worker"):
            i.retire("w1", specs=None)
        assert i.get("w1") is not None


class TestTheCliCommand:
    """`python -m control retire-worker <host_id>`: prints what it did, and
    audits it either way (design 18.4) - a refused destroy is exactly what an
    operator needs to find later."""

    def db(self, tmp_path):
        path = str(tmp_path / "c.db")
        schema.init(path)
        return path

    def test_it_retires_and_records_an_accepted_audit_row(self, tmp_path):
        path = self.db(tmp_path)
        inv.Inventory(path).register_worker(
            "wsl-linux-1", inv.HYPERV_LINUX,
            capabilities={"kind": "linux-container"})
        assert main.retire_worker("wsl-linux-1", db=path) == "wsl-linux-1"
        assert inv.Inventory(path).get("wsl-linux-1") is None

        rows = audit.entries(path, verb="retire_worker")
        assert len(rows) == 1
        assert rows[0]["decision"] == "accepted"
        assert rows[0]["actor"] == "cli"
        assert rows[0]["parameters"] == '{"host_id": "wsl-linux-1"}'

    def test_a_refusal_is_audited_with_how_many_runners_name_it(
            self, tmp_path):
        path = self.db(tmp_path)
        inv.Inventory(path).register_worker(
            "w1", inv.HYPERV_LINUX, capabilities={"kind": "linux-container"})
        SpecStore(path).create(provider="github", platform="linux",
                               host_id="w1", desired_state="running",
                               actual_state="idle")

        with pytest.raises(ValueError, match="names this worker"):
            main.retire_worker("w1", db=path)
        assert inv.Inventory(path).get("w1") is not None

        rows = audit.entries(path, verb="retire_worker")
        assert len(rows) == 1
        assert rows[0]["decision"] == "refused"
        assert "names this worker" in rows[0]["outcome"]

    def test_the_cli_subcommand_is_wired_up(self, tmp_path, monkeypatch,
                                            capsys):
        path = self.db(tmp_path)
        inv.Inventory(path).register_worker(
            "wsl-linux-1", inv.HYPERV_LINUX,
            capabilities={"kind": "linux-container"})
        monkeypatch.setattr(main, "_store", lambda db=None: path)
        assert main.main(["retire-worker", "wsl-linux-1"]) == 0
        assert "wsl-linux-1" in capsys.readouterr().out
        assert inv.Inventory(path).get("wsl-linux-1") is None

    def test_the_cli_reports_a_refusal_and_exits_nonzero(
            self, tmp_path, monkeypatch, capsys):
        path = self.db(tmp_path)
        inv.Inventory(path).register_worker(
            "w1", inv.HYPERV_LINUX, capabilities={"kind": "linux-container"})
        SpecStore(path).create(provider="github", platform="linux",
                               host_id="w1", desired_state="running",
                               actual_state="idle")
        monkeypatch.setattr(main, "_store", lambda db=None: path)
        assert main.main(["retire-worker", "w1"]) == 2
        out = capsys.readouterr().out
        assert "refused" in out
        assert "names this worker" in out
        assert inv.Inventory(path).get("w1") is not None

    def test_an_unknown_host_id_is_a_refusal_not_a_success(self, tmp_path):
        path = self.db(tmp_path)
        with pytest.raises(inv.UnknownWorker):
            main.retire_worker("no-such-worker", db=path)
        rows = audit.entries(path, verb="retire_worker")
        assert len(rows) == 1
        assert rows[0]["decision"] == "refused"
        assert "no-such-worker" in rows[0]["outcome"]

    def test_the_cli_reports_an_unknown_worker_as_a_refusal(
            self, tmp_path, monkeypatch, capsys):
        path = self.db(tmp_path)
        monkeypatch.setattr(main, "_store", lambda db=None: path)
        assert main.main(["retire-worker", "no-such-worker"]) == 2
        out = capsys.readouterr().out
        assert "refused" in out
        assert "no-such-worker" in out

    def test_a_failing_audit_write_does_not_undo_a_completed_retire(
            self, tmp_path, monkeypatch):
        """`retire_worker` must not let a broken audit table change what
        happened to the worker - `RunnerService._audit`'s rule for every
        other verb applies here too."""
        path = self.db(tmp_path)
        inv.Inventory(path).register_worker(
            "wsl-linux-1", inv.HYPERV_LINUX,
            capabilities={"kind": "linux-container"})

        def broken(*a, **kw):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(audit, "record", broken)
        assert main.retire_worker("wsl-linux-1", db=path) == "wsl-linux-1"
        assert inv.Inventory(path).get("wsl-linux-1") is None
