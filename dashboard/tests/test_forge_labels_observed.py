"""What the forge lists as a runner's labels is kept on its spec."""
import sqlite3

from store import schema
from tests.test_reconciler import GH, converge, live, world  # noqa: F401


def test_an_observed_runner_keeps_the_labels_its_forge_lists(world, monkeypatch):
    service, executor, reconciler = world
    service.scale_up(GH)
    converge(service, reconciler)
    spec = live(service)[0]
    executor.world[spec["runner_id"]] = "idle"
    executor.labels_seen = {spec["runner_id"]: ["self-hosted", "Linux", "X64", "beast-unit"]}
    reconciler.pass_once()
    stored = service.specs.get(spec["runner_id"])
    assert stored["forge_labels"] == ["self-hosted", "Linux", "X64", "beast-unit"]
    assert stored["forge_labels_at"]


def test_unknown_labels_leave_what_was_stored(world):
    service, executor, reconciler = world
    service.scale_up(GH)
    converge(service, reconciler)
    spec = live(service)[0]
    executor.world[spec["runner_id"]] = "idle"
    executor.labels_seen = {spec["runner_id"]: ["a"]}
    reconciler.pass_once()
    executor.labels_seen = {spec["runner_id"]: None}
    reconciler.pass_once()
    assert service.specs.get(spec["runner_id"])["forge_labels"] == ["a"]


def test_the_columns_are_added_to_an_existing_store(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.executescript(schema.SCHEMA.replace("  forge_labels     TEXT,\n", "").replace(
        "  forge_labels_at  TEXT,\n", ""))
    c.close()
    schema.init(path)
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(runner_specs)")}
    assert {"forge_labels", "forge_labels_at"} <= cols
