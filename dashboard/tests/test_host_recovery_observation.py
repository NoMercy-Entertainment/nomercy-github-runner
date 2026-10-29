"""Slow worker reads must not make a confirmed shutdown permanently stale."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

import providers
from control.provision import ProvisioningFlow
from control.reconciler import Reconciler, Report
from store import schema
from tests.test_forge_health import plant, NOW, stamp  # noqa: F401


def setup_flow(plant, monkeypatch, latest="offline", running=False):
    service, rid = plant
    flow = ProvisioningFlow(service, None, None)
    monkeypatch.setattr(service.inventory, "accepts_destructive_verbs", lambda host: True)
    completed = []
    new_stamp = stamp(NOW + timedelta(seconds=10))

    def status(ref):
        # Routine forge health advances while a slow worker answers.
        with schema.connect(service.specs.path) as c:
            c.execute("UPDATE runner_specs SET forge_state=?,forge_seen_at=? WHERE runner_id=?", (latest, new_stamp, rid))
        completed.append(True)
        return SimpleNamespace(exists=True, running=running)

    def records(provider, fresh=False, spec=None):
        state = latest if completed else "offline"
        flow._observed_at[provider.key] = new_stamp if completed else stamp(NOW)
        return [{"id": 1, "name": "owned-runner", "status": state, "busy": state == "busy", "labels": []}]

    monkeypatch.setattr(flow, "_runtime", lambda spec: SimpleNamespace(status=status))
    monkeypatch.setattr(flow, "_records", records)
    return service, rid, flow


def test_confirmed_shutdown_survives_health_refresh_during_slow_worker_read(plant, monkeypatch):
    service, rid, flow = setup_flow(plant, monkeypatch)
    report = Report()
    Reconciler(service, flow)._do_observe(service.specs.get(rid), None, report)
    assert service.specs.get(rid)["actual_state"] == "stopped"
    assert report.actions == [("observed", rid, "stopped")]


@pytest.mark.parametrize("latest", ["busy", "idle", "unknown"])
def test_newer_non_offline_forge_answer_cannot_confirm_shutdown(plant, monkeypatch, latest):
    service, rid, flow = setup_flow(plant, monkeypatch, latest=latest)
    assert not flow._observed_shutdown(service.specs.get(rid), providers.by_key("github"))


@pytest.mark.parametrize("running", [True, None])
def test_running_or_unknown_unit_cannot_confirm_shutdown(plant, monkeypatch, running):
    service, rid, flow = setup_flow(plant, monkeypatch, running=running)
    assert not flow._observed_shutdown(service.specs.get(rid), providers.by_key("github"))
