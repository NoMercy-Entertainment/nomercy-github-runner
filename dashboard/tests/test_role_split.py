"""T-1902: read, operate and destroy, per design 18.2 - and destroy is the
admin's.

Destroy is remove, recreate, deregister, fleet recreate and a capacity
decrease. Done when a non-admin cannot remove a runner or reduce capacity -
by any route, v1 included, since a v1 route that let an operator remove would
make the rule untrue. A refused destroy is recorded with its actor.
"""
import pytest

import api_v2
from control import audit
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore

GH = "github-linux-x64"
RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


@pytest.fixture
def plane(tmp_path, monkeypatch):
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed({})
    monkeypatch.setattr(api_v2, "_db_path", lambda: path)
    monkeypatch.setattr(schema, "DB_PATH", path)
    monkeypatch.setitem(api_v2._status, "fn", lambda: {})
    service = RunnerService(path)
    service.set_capacity(GH, 2)
    rid = service.planned_ids(service.plan(GH, 1))[0]
    spec = service.specs.get(rid)
    service.specs.update(rid, spec["spec_version"], actual_state="stopped")
    return service, rid


def as_role(role):
    import users
    users.approve("sub-test-admin", role)


def post(client, path, body=None):
    return client.post(path, json=body or {},
                       headers={"Idempotency-Key": f"k-{path}-{body}"})


class TestAnOperatorCannotDestroy:
    @pytest.mark.parametrize("verb", ["remove", "recreate", "deregister"])
    def test_a_runner(self, client, plane, verb):
        as_role("operator")
        service, rid = plane
        r = post(client, f"/api/v2/runners/{rid}/actions/{verb}")
        assert r.status_code == 403
        assert service.specs.get(rid)["current_operation"] is None

    def test_a_fleet(self, client, plane):
        as_role("operator")
        assert post(client, f"/api/v2/fleets/{GH}/recreate").status_code == 403

    def test_nor_reduce_capacity(self, client, plane):
        as_role("operator")
        service, _ = plane
        r = post(client, f"/api/v2/fleets/{GH}/capacity", {"desired": 1})
        assert r.status_code == 403
        assert service.fleets.get(GH)["desired_capacity"] == 2

    @pytest.mark.parametrize("path", ["/api/runner/remove",
                                      "/api/v1/runner/remove",
                                      "/api/recreate", "/api/v1/recreate"])
    def test_nor_through_v1(self, client, plane, path):
        as_role("operator")
        r = client.post(path, json={"name": "github-runner-1",
                                    "provider": "github"})
        assert r.status_code == 403


class TestAnOperatorCanOperate:
    def test_start_a_runner(self, client, plane):
        as_role("operator")
        service, rid = plane
        assert post(client, f"/api/v2/runners/{rid}/actions/start"
                    ).status_code == 202

    def test_raise_capacity_and_add_a_runner(self, client, plane):
        as_role("operator")
        assert post(client, f"/api/v2/fleets/{GH}/capacity",
                    {"desired": 3}).status_code == 202
        assert post(client, f"/api/v2/fleets/{GH}/runners").status_code == 202

    def test_a_v1_operate_route_is_not_refused_by_role(self, client, plane):
        """Refused for its bad name, which is the route's own check - not
        by the guard."""
        as_role("operator")
        r = client.post("/api/runner/stop", json={"name": "not-a-runner"})
        assert r.status_code == 400


class TestAnAdminCan:
    def test_remove(self, client, plane):
        as_role("admin")
        service, rid = plane
        assert post(client, f"/api/v2/runners/{rid}/actions/remove"
                    ).status_code == 202

    def test_reduce_capacity(self, client, plane):
        as_role("admin")
        service, _ = plane
        assert post(client, f"/api/v2/fleets/{GH}/capacity",
                    {"desired": 1}).status_code == 202
        assert service.fleets.get(GH)["desired_capacity"] == 1


class TestARefusedDestroyIsRecorded:
    def test_with_its_actor_and_why(self, client, plane):
        as_role("operator")
        service, rid = plane
        post(client, f"/api/v2/runners/{rid}/actions/remove")
        post(client, f"/api/v2/fleets/{GH}/capacity", {"desired": 0})
        refused = audit.entries(service.operations.path, decision="refused")
        outcomes = [r["outcome"] for r in refused]
        assert any("requires admin; the caller is operator" in o
                   for o in outcomes)
        assert any("a decrease requires admin" in o for o in outcomes)


def test_a_viewer_still_cannot_post_anything(client, plane):
    as_role("viewer")
    service, rid = plane
    assert post(client, f"/api/v2/runners/{rid}/actions/start"
                ).status_code == 403
