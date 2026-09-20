"""T-1403 and T-1404: the fleets, operable, and rows rather than code.

Capacity, add, recreate and clear-cache per fleet, each an operation with an
idempotency key; scale up and scale down are the same capacity call. The
fleets themselves come from the table - six by seeding, and a seventh the
moment it is a row - and a fleet that cannot exist says why.
"""
import pytest

import api_v2
import providers as P
from control.service import RunnerService
from store import schema
from store.fleets import FleetStore

BUILT = {"FORGEJO_RUNNER_ARTIFACT_WINDOWS": "forgejo-runner.exe",
         "FORGEJO_RUNNER_ARTIFACT_MACOS": "forgejo-runner-darwin-amd64"}
GH = "github-linux-x64"


@pytest.fixture
def plane(tmp_path, monkeypatch):
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed(BUILT)
    monkeypatch.setattr(api_v2, "_db_path", lambda: path)
    monkeypatch.setitem(api_v2._status, "fn", lambda: {})
    return RunnerService(path), path


def post(client, url, key="k-1", body=None):
    return client.post(url, json=body or {},
                       headers={"Idempotency-Key": key} if key else {})


def desired(service, fid=GH):
    return service.fleets.get(fid)["desired_capacity"]


class TestCapacity:
    def test_up_and_down_are_one_call(self, client, plane):
        service, _ = plane
        assert post(client, f"/api/v2/fleets/{GH}/capacity", "a",
                    {"desired": 3}).status_code == 202
        assert desired(service) == 3
        assert post(client, f"/api/v2/fleets/{GH}/capacity", "b",
                    {"desired": 1}).status_code == 202
        assert desired(service) == 1

    def test_a_repeat_does_not_undo_a_change_made_since(self, client, plane):
        service, _ = plane
        post(client, f"/api/v2/fleets/{GH}/capacity", "first", {"desired": 3})
        post(client, f"/api/v2/fleets/{GH}/capacity", "second",
             {"desired": 5})
        again = post(client, f"/api/v2/fleets/{GH}/capacity", "first",
                     {"desired": 3})
        assert again.status_code == 202
        assert desired(service) == 5

    @pytest.mark.parametrize("bad", [-1, "3", 1.5, True, None])
    def test_a_number_that_is_not_one_is_refused(self, client, plane, bad):
        r = post(client, f"/api/v2/fleets/{GH}/capacity", body={"desired": bad})
        assert r.status_code == 400

    def test_a_fleet_that_cannot_exist_is_refused_with_its_reason(
            self, client, tmp_path, monkeypatch):
        path = str(tmp_path / "bare.db")
        schema.init(path)
        FleetStore(path).seed({})          # no self-built artefacts
        monkeypatch.setattr(api_v2, "_db_path", lambda: path)
        r = post(client, "/api/v2/fleets/forgejo-windows-x64/capacity",
                 body={"desired": 1})
        assert r.status_code == 409
        assert "FORGEJO_RUNNER_ARTIFACT_WINDOWS" in r.get_json()["error"]

    def test_an_unknown_fleet(self, client, plane):
        r = post(client, "/api/v2/fleets/nope/capacity", body={"desired": 1})
        assert r.status_code == 404


class TestAddARunner:
    def test_it_is_one_more_capacity(self, client, plane):
        service, _ = plane
        before = desired(service)
        assert post(client, f"/api/v2/fleets/{GH}/runners",
                    "add-1").status_code == 202
        assert desired(service) == before + 1

    def test_sent_twice_it_adds_one(self, client, plane):
        """The caller who did not hear the answer and asked again."""
        service, _ = plane
        before = desired(service)
        post(client, f"/api/v2/fleets/{GH}/runners", "add-once")
        post(client, f"/api/v2/fleets/{GH}/runners", "add-once")
        assert desired(service) == before + 1


def with_runners(service, states_):
    rids = service.planned_ids(service.plan(GH, len(states_), env=BUILT))
    for rid, state in zip(rids, states_):
        spec = service.specs.get(rid)
        service.specs.update(rid, spec["spec_version"], actual_state=state,
                             capabilities={"clear_cache": True})
    return rids


class TestRecreateAndClear:
    def test_recreate_rebuilds_every_runner_the_fleet_has(self, client,
                                                          plane):
        """Recreating a fleet means the runners it has, whatever each of
        them is doing. One that is serving is drained first and rebuilt when
        its job is done - the reconciler has always known how; it was this
        gate that refused it with a state machine's words (2026-09-20)."""
        service, _ = plane
        drained, idle = with_runners(service, ["drained", "idle"])
        body = post(client, f"/api/v2/fleets/{GH}/recreate", "r").get_json()
        by_id = {r["runner_id"]: r for r in body["results"]}
        assert by_id[drained]["ok"] is True
        assert by_id[idle]["ok"] is True

    def test_a_runner_that_cannot_be_recreated_says_why(self, client, plane):
        """Not everything is walkable: a runner already on its way out has
        nothing to rebuild."""
        service, _ = plane
        gone, = with_runners(service, ["absent"])
        body = post(client, f"/api/v2/fleets/{GH}/recreate", "r2").get_json()
        by_id = {r["runner_id"]: r for r in body["results"]}
        assert by_id[gone]["ok"] is False and by_id[gone]["error"]

    def test_clear_cache_skips_a_busy_runner_with_the_reason(self, client,
                                                             plane):
        service, _ = plane
        idle, busy = with_runners(service, ["idle", "busy"])
        body = post(client, f"/api/v2/fleets/{GH}/clear-cache",
                    "c").get_json()
        by_id = {r["runner_id"]: r for r in body["results"]}
        assert by_id[idle]["ok"] is True
        assert "only idle or drained" in by_id[busy]["skipped"]
        assert service.specs.get(busy)["current_operation"] is None

    def test_repeating_either_repeats_nothing(self, client, plane):
        service, _ = plane
        (idle,) = with_runners(service, ["idle"])
        first = post(client, f"/api/v2/fleets/{GH}/clear-cache", "same")
        again = post(client, f"/api/v2/fleets/{GH}/clear-cache", "same")
        assert first.get_json()["results"][0]["operation_id"] == \
            again.get_json()["results"][0]["operation_id"]


class TestTheFleetsAreRows:
    def test_without_a_control_plane_the_six_come_from_the_providers(
            self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(api_v2, "_db_path",
                            lambda: str(tmp_path / "none.db"))
        monkeypatch.setitem(api_v2._status, "fn", lambda: {})
        fleets = client.get("/api/v2/fleets").get_json()["fleets"]
        assert len(fleets) == 6
        win = next(f for f in fleets if f["fleet_id"] == "forgejo-windows-x64")
        assert win["available"] is False
        assert "FORGEJO_RUNNER_ARTIFACT_WINDOWS" in win["reason"]
        assert all(not a["enabled"] and a["reason"] for a in win["actions"])

    def test_a_seventh_fleet_needs_no_code_only_a_row(self, client, plane):
        service, path = plane
        with schema.connect(path) as c:
            c.execute("INSERT INTO fleets (fleet_id, provider, platform,"
                      " architecture, desired_capacity, labels, available)"
                      " VALUES ('github-linux-arm64', 'github', 'linux',"
                      " 'arm64', 0, '[]', 1)")
        fleets = client.get("/api/v2/fleets").get_json()["fleets"]
        ids = [f["fleet_id"] for f in fleets]
        assert "github-linux-arm64" in ids and len(ids) == 7
        arm = next(f for f in fleets if f["fleet_id"] == "github-linux-arm64")
        assert arm["title"] == "GitHub · Linux · arm64"
        assert any(a["enabled"] for a in arm["actions"])

    def test_a_fleet_reads_by_its_id(self, client, plane):
        r = client.get(f"/api/v2/fleets/{GH}")
        assert r.status_code == 200
        assert r.get_json()["fleet"]["fleet_id"] == GH

    def test_the_v1_fleets_keep_their_v1_actions_while_v1_serves_them(
            self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(api_v2, "_db_path",
                            lambda: str(tmp_path / "none.db"))
        monkeypatch.setitem(api_v2._status, "fn", lambda: {
            "providers_configured": {"github": True, "forgejo": False}})
        fleets = {f["fleet_id"]: f for f in
                  client.get("/api/v2/fleets").get_json()["fleets"]}
        add = next(a for a in fleets[GH]["actions"] if a["verb"] == "add")
        assert (add["url"], add["body"]) == ("/api/runner/add",
                                             {"provider": "github"})
        fj = next(a for a in fleets["forgejo-linux-x64"]["actions"]
                  if a["verb"] == "add")
        assert fj["enabled"] is False, "Forgejo is not configured in v1"


class TestThePageOffersNoCapacity:
    """A fleet has the runners you add and keeps them until you remove one.
    The number went from the page on 2026-09-20; its button did not, and
    pressing it posted no number at all: "Capacity failed - desired must be
    a whole number, 0 or more" (2026-09-21). The route stays for scripts.
    """

    def verbs(self, client):
        return {f["fleet_id"]: [a["verb"] for a in f["actions"]]
                for f in client.get("/api/v2/fleets").get_json()["fleets"]}

    def test_with_the_control_plane(self, client, plane):
        for fid, verbs in self.verbs(client).items():
            assert "capacity" not in verbs, fid

    def test_and_on_the_fleets_v1_still_serves(self, client, tmp_path,
                                               monkeypatch):
        monkeypatch.setattr(api_v2, "_db_path",
                            lambda: str(tmp_path / "none.db"))
        monkeypatch.setitem(api_v2._status, "fn", lambda: {
            "providers_configured": {"github": True, "forgejo": True}})
        for fid, verbs in self.verbs(client).items():
            assert "capacity" not in verbs, fid

    def test_every_fleet_offers_the_same_three(self, client, plane):
        for fid, verbs in self.verbs(client).items():
            assert verbs == list(api_v2.FLEET_ACTIONS), fid

    def test_the_page_has_nothing_left_that_asks_for_a_number(self):
        import os
        page = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "templates", "fleet_v2.html")
        with open(page, encoding="utf-8") as fh:
            assert "capacity" not in fh.read().lower()


class TestACellSaysWhatItsWorkersCanBuild:
    """A forge supporting a platform is not enough to make a runner: some
    worker has to be able to build one. A worker that makes units from
    templates on its own disk lists them; one that makes them from images
    can build anything it can pull.

    Without this the page offered `+ Add runner` for GitHub on Windows and
    macOS, where the only template installed was Forgejo's, and the creation
    could only fail on the worker (2026-09-20).
    """

    def service(self, tmp_path, workers, env=None):
        from control.service import RunnerService
        from store import schema
        from store.fleets import FleetStore
        env = BUILT if env is None else env
        path = str(tmp_path / "control.db")
        schema.init(path)
        FleetStore(path).seed(env)
        from control import agent_runtime
        service = RunnerService(path, env=env,
                                runtimes=agent_runtime.TABLE)
        for host_id, kind, caps in workers:
            service.inventory.register_worker(host_id, kind,
                                              capabilities=caps)
            service.inventory.heartbeat(host_id, capabilities=caps)
        return service

    WINDOWS_WITH_FORGEJO = ("beast-unit", "hyperv-windows",
                            {"kind": "windows-process",
                             "builds_from": "template",
                             "templates": ["forgejo-runner.exe"]})
    LINUX_ENGINE = ("wsl-linux-1", "hyperv-linux",
                    {"kind": "linux-container", "builds_from": "image"})

    def test_a_template_no_worker_has_is_not_buildable(self, tmp_path):
        service = self.service(tmp_path, [self.WINDOWS_WITH_FORGEJO])
        ok, reason = service.buildable("github-windows-x64")
        assert not ok
        assert "actions/runner" in reason or "template" in reason

    def test_the_reason_reads_as_a_sentence(self, tmp_path):
        """It is shown to whoever is looking at the page. A worker with no
        template at all printed "the ones that could hold it have []", which
        is a Python list on a fleet's card (2026-09-21)."""
        bare = ("appliance-1", "hyperv-linux",
                {"kind": "macos-appliance", "builds_from": "template",
                 "templates": []})
        service = self.service(tmp_path, [bare])
        ok, reason = service.buildable("forgejo-macos-x64")
        assert not ok
        assert "[]" not in reason and "'" not in reason.split("template ")[-1][:1]
        assert "none" in reason

    def test_a_fleet_that_names_no_template_says_that(self, tmp_path):
        """Forgejo on macOS has no artefact named until one is built, so
        there is no template to look for - and the sentence read "no worker
        has the template  this fleet is made from", with a hole where the
        name should be (2026-09-21)."""
        bare = ("appliance-1", "hyperv-linux",
                {"kind": "macos-appliance", "builds_from": "template",
                 "templates": []})
        unnamed = {k: v for k, v in BUILT.items()
                   if k != "FORGEJO_RUNNER_ARTIFACT_MACOS"}
        service = self.service(tmp_path, [bare], env=unnamed)
        _, reason = service.buildable("forgejo-macos-x64")
        assert "  " not in reason
        assert "names no template" in reason

    def test_the_reason_names_what_the_workers_do_have(self, tmp_path):
        service = self.service(tmp_path, [self.WINDOWS_WITH_FORGEJO])
        _, reason = service.buildable("github-windows-x64")
        assert "forgejo-runner.exe" in reason and "[" not in reason

    def test_a_template_a_worker_has_is_buildable(self, tmp_path):
        service = self.service(tmp_path, [self.WINDOWS_WITH_FORGEJO])
        ok, _ = service.buildable("forgejo-windows-x64")
        assert ok

    def test_a_worker_that_builds_from_images_can_build_anything(self,
                                                                 tmp_path):
        service = self.service(tmp_path, [self.LINUX_ENGINE])
        assert service.buildable("github-linux-x64")[0]
        assert service.buildable("forgejo-linux-x64")[0]

    def test_no_worker_at_all_is_a_note_not_a_refusal(self, tmp_path):
        """A worker that is down comes back, and a runner planned meanwhile
        waits for one. Only a template nobody has is a refusal."""
        service = self.service(tmp_path, [])
        ok, reason = service.buildable("github-windows-x64")
        assert ok
        assert "worker" in reason

    def test_planning_one_is_refused_with_that_reason(self, tmp_path):
        from control.service import Refused
        service = self.service(tmp_path, [self.WINDOWS_WITH_FORGEJO])
        with pytest.raises(Refused, match="template"):
            service.plan("github-windows-x64", 1)


    def test_the_page_shows_the_cell_as_unavailable(self, tmp_path):
        import api_v2
        service = self.service(tmp_path, [self.WINDOWS_WITH_FORGEJO])
        rows = {f["fleet_id"]: f
                for f in api_v2.fleet_list(service, None, [])}
        assert rows["github-windows-x64"]["available"] is False
        assert "template" in rows["github-windows-x64"]["reason"]
        assert rows["forgejo-windows-x64"]["available"] is True

    def test_the_add_button_is_disabled_with_that_reason(self, tmp_path):
        import api_v2
        service = self.service(tmp_path, [self.WINDOWS_WITH_FORGEJO])
        rows = {f["fleet_id"]: f
                for f in api_v2.fleet_list(service, None, [])}
        add = rows["github-windows-x64"]["actions"][0]
        assert add["enabled"] is False
        assert "template" in (add["reason"] or "")


    def test_the_page_uses_the_deployments_settings(self, monkeypatch,
                                                    tmp_path):
        """What a unit of a cell is made from is a setting. A service built
        without them reads every cell as unbuildable, which is how GitHub on
        Windows stayed unavailable after its template was installed."""
        import api_v2
        made = self.service(tmp_path, [])
        monkeypatch.setattr(api_v2, "_db_path", lambda: made.specs.path)
        monkeypatch.setenv("RUNNER_UNIT_IMAGE_GITHUB_WINDOWS", "a-template")
        service, _ = api_v2.control_plane()
        assert service is not None
        fleet = service.fleets.get("github-windows-x64")
        assert service.unit_image(fleet) == "a-template"
