"""Maintenance survives restart; heartbeat reports actual process health."""
from types import SimpleNamespace

from control.main import Controller
from control.secrets import SecretStore
from store import schema


def test_maintenance_blocks_reconcile_and_refreshes_secret(tmp_path):
    db = str(tmp_path / "control.db")
    schema.init(db)
    SecretStore(db).set("GH_TOKEN", "updated-token-value", "admin")
    with schema.connect(db) as c:
        c.execute("INSERT INTO platform_settings VALUES ('maintenance','true')")
    controller = Controller.__new__(Controller)
    controller.db, controller.base_env = db, {"GH_TOKEN": "old-token-value"}
    controller.service = SimpleNamespace(env={})
    controller.flow = SimpleNamespace(env={}, forges=SimpleNamespace(env={}))
    controller.reconciler = SimpleNamespace(pass_once=lambda: 1 / 0)
    report = controller.pass_once()
    assert report.actions == []
    assert report.held == [("platform", "maintenance")]
    assert controller.flow.forges.env["GH_TOKEN"] == "updated-token-value"
    assert controller.service.env == controller.flow.env


def test_heartbeat_has_fresh_timestamp_and_explicit_stop(tmp_path):
    db = str(tmp_path / "control.db")
    schema.init(db)
    controller = Controller.__new__(Controller)
    controller.db, controller._last_error = db, None
    controller._heartbeat()
    with schema.connect(db) as c:
        row = dict(c.execute("SELECT * FROM controller_status").fetchone())
    assert row["state"] == "running"
    assert row["last_seen_at"].endswith("Z")
    controller._heartbeat("stopped")
    with schema.connect(db) as c:
        assert c.execute("SELECT state FROM controller_status").fetchone()[0] == "stopped"


def test_receiver_closes_even_if_final_heartbeat_cannot_write():
    import threading
    import pytest
    controller = Controller.__new__(Controller)
    controller._stop = threading.Event()
    controller._stop.set()
    controller.interval = 15
    closed = []
    controller.receiver = SimpleNamespace(start=lambda: None, stop=lambda: closed.append(True),
                                          server_address=("127.0.0.1", 8444))
    def failed_write(state=None):
        raise OSError("disk unavailable")
    controller._heartbeat = failed_write
    with pytest.raises(OSError, match="disk unavailable"):
        controller.run(log=lambda _: None)
    assert closed == [True]
