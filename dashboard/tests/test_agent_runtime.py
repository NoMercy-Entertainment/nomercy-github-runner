"""The controller's adapters to a worker, and the authority it runs on.

The wire itself is proven in test_controller_process.py. These pin down the
rules that are easy to break quietly: what a unit spec may carry to a worker,
that a runner with no worker is refused rather than sent somewhere, and that
the control plane's authority is made once and never replaced.
"""
import os

import pytest

from control import agent_runtime as ar
from control import main
from runtime.base import ExecUnitKind, ExecUnitRef, Probe

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
REF = ExecUnitRef(kind=ExecUnitKind("linux-container"), handle=f"rnr-{RID}")


class Client:
    def __init__(self, answers=None):
        self.calls = []
        self.answers = answers or {}

    def call_and_wait(self, host_id, verb, body, **kw):
        self.calls.append((host_id, verb, body))
        return self.answers.get(verb, {})


def runtime(answers=None, images=None):
    client = Client(answers)
    return ar.AgentRuntime(ar.AgentWiring(client, images=images or {}),
                           "linux-1"), client


class TestTheUnitSpec:
    SPEC = {"runner_id": RID, "provider": "github", "platform": "linux",
            "fleet_id": "github-linux-x64", "cpu_limit": "8",
            "memory_limit": 32 * 2 ** 30, "labels": ["self-hosted", "x"],
            "registration_id": "41", "runtime_template": "actions/runner@v2",
            "display_name": "runner", "cache_policy": {"on_clear": "skip"}}

    def test_only_what_the_verb_takes_is_sent(self):
        rt, client = runtime({"exec_unit.create": {"handle": f"rnr-{RID}"}},
                             images={("github", "linux"): "ghcr.io/x/u:1"})
        ref = rt.create(self.SPEC)
        (_, verb, body), = client.calls
        assert verb == "exec_unit.create"
        assert set(body) == {"runner_id", "spec"}
        assert body["spec"] == {
            "image": "ghcr.io/x/u:1", "cpus": "8",
            "memory": str(32 * 2 ** 30),
            "labels": {"nomercy.provider": "github",
                       "nomercy.fleet": "github-linux-x64"}}
        assert ref.handle == f"rnr-{RID}"

    def test_the_fleet_template_when_no_image_is_configured(self):
        rt, client = runtime({"exec_unit.create": {"handle": f"rnr-{RID}"}})
        rt.create(self.SPEC)
        assert client.calls[0][2]["spec"]["image"] == "actions/runner@v2"

    def test_a_unit_gets_its_cells_memory_limit_when_it_names_none(self):
        client = Client({"exec_unit.create": {"handle": f"rnr-{RID}"}})
        rt = ar.AgentRuntime(ar.AgentWiring(
            client, memory={("github", "linux"): "6g"}), "linux-1")
        rt.create(dict(self.SPEC, memory_limit=None))
        assert client.calls[0][2]["spec"]["memory"] == "6g"
        client.calls.clear()
        rt.create(self.SPEC)
        assert client.calls[0][2]["spec"]["memory"] == str(32 * 2 ** 30), \
            "the runner's own limit wins"

    def test_nothing_to_make_it_from_is_refused_before_sending(self):
        rt, client = runtime()
        with pytest.raises(ar.NotBound, match="made from"):
            rt.create(dict(self.SPEC, runtime_template=None))
        assert client.calls == []


class TestWhereAUnitIs:
    def test_a_runner_with_no_worker_is_refused(self):
        class Service:
            agents = ar.AgentWiring(Client())
        with pytest.raises(ar.NotBound, match="no worker"):
            ar.AgentRuntime.for_runner(Service(), {"runner_id": RID})

    def test_a_controller_with_no_client_is_refused(self):
        class Service:
            agents = None
        with pytest.raises(ar.NotBound, match="no agent client"):
            ar.AgentRuntime.for_runner(Service(), {"host_id": "linux-1"})

    def test_it_goes_to_the_runners_own_worker(self):
        class Service:
            agents = ar.AgentWiring(Client())
        bound = ar.AgentRuntime.for_runner(Service(), {"host_id": "linux-7"})
        bound.start(REF)
        assert Service.agents.client.calls[0][:2] == ("linux-7",
                                                      "exec_unit.start")

    @pytest.mark.parametrize("handle", ["github-runner-3", "rnr-../../x",
                                        f"rnr-{RID}; rm"])
    def test_a_handle_this_controller_did_not_make_is_not_sent(self, handle):
        rt, client = runtime()
        with pytest.raises(ar.NotBound):
            rt.stop(ExecUnitRef(kind=ExecUnitKind("linux-container"),
                                handle=handle))
        assert client.calls == []


class TestReads:
    def test_unknown_is_not_absent(self):
        """The flow would build a second unit on an "absent" it misread."""
        rt, _ = runtime({"exec_unit.status": {"exists": None,
                                              "running": None}})
        with pytest.raises(RuntimeError, match="could not say"):
            rt.status(REF)

    def test_a_probe_is_a_member_never_a_string(self):
        rt, client = runtime({"exec_unit.probe": {"ok": True, "value": 7}})
        assert rt.exec_probe(REF, Probe.DISK_USAGE).value == 7
        assert client.calls[0][2] == {"runner_id": RID,
                                      "probe": "disk_usage"}
        with pytest.raises(TypeError):
            rt.exec_probe(REF, "df -h")

    def test_a_cache_policy_sends_only_its_own_fields(self):
        rt, client = runtime({"exec_unit.clear_cache": {"total_bytes": 5,
                                                        "measured": True}})
        freed = rt.clear_cache(REF, {"scopes": ["workspace"], "owner": "x"})
        assert client.calls[0][2]["policy"] == {"scopes": ["workspace"]}
        assert freed.total_bytes == 5 and freed.measured


class TestTheFlowsAgent:
    def test_a_plan_goes_without_its_extras(self):
        import providers as P
        client = Client({"runner.register": {"registration_id": 41}})
        agent = ar.FlowAgent(ar.AgentWiring(client))
        plan = P.RegistrationPlan(url="https://github.com/x", token="t0k",
                                  name="r", labels="a,b",
                                  extra=(("x", "y"),))
        assert agent.register("linux-1", REF, plan)["registration_id"] == \
            "41"
        assert set(client.calls[0][2]["plan"]) == set(
            ar.FlowAgent.PLAN_FIELDS)

    def test_running_unknown_proves_nothing(self):
        agent = ar.FlowAgent(ar.AgentWiring(Client(
            {"exec_unit.status": {"exists": None, "running": None}})))
        with pytest.raises(RuntimeError):
            agent.running("linux-1", REF)


class TestTheAuthority:
    def test_it_is_made_once(self, tmp_path):
        tls = str(tmp_path / "tls")
        first = main.init_pki(tls)
        with open(os.path.join(tls, "ca.pem"), "rb") as fh:
            ca = fh.read()
        second = main.init_pki(tls)
        assert first == {"authority": "made",
                         "made": ["controller", "receiver"]}
        assert second == {"authority": "kept", "made": []}
        with open(os.path.join(tls, "ca.pem"), "rb") as fh:
            assert fh.read() == ca

    def test_certificates_without_their_authority_are_not_papered_over(
            self, tmp_path):
        tls = str(tmp_path / "tls")
        main.init_pki(tls)
        os.unlink(os.path.join(tls, "ca.pem"))
        with pytest.raises(RuntimeError, match="without their authority"):
            main.init_pki(tls)

    def test_a_worker_is_enrolled_pinned_to_the_certificate_it_got(
            self, tmp_path):
        from control import ca
        from control.inventory import Inventory
        from store import schema
        tls, db = str(tmp_path / "tls"), str(tmp_path / "c.db")
        schema.init(db)
        main.init_pki(tls)
        bundle = main.enrol("linux-1", "hyperv-linux",
                            "https://10.77.0.20:8443", db=db, tls_dir=tls)
        with open(os.path.join(bundle, "agent.crt"), "rb") as fh:
            pinned = ca.fingerprint(fh.read())
        worker = Inventory(db).get("linux-1")
        assert worker["certificate_fingerprint"] == pinned
        assert worker["endpoint"] == "https://10.77.0.20:8443"
        assert sorted(os.listdir(bundle)) == ["agent.crt", "agent.key",
                                              "ca.pem"]
        assert not os.path.exists(os.path.join(bundle, "ca.key"))

    def test_every_command_works_on_a_store_nothing_has_made_yet(
            self, tmp_path):
        """Found running it in the controller's image: an enrolment that
        beats the controller's first start failed on a missing table."""
        tls, db = str(tmp_path / "tls"), str(tmp_path / "fresh.db")
        main.init_pki(tls)
        main.enrol("linux-1", "hyperv-linux", "https://10.77.0.20:8443",
                   db=db, tls_dir=tls)
        assert main.status(db=str(tmp_path / "other.db")).startswith(
            "workers:")
        assert "linux-1" in main.status(db=db)
        main.capacity("forgejo-linux-x64", 0, db=str(tmp_path / "third.db"))

    def test_a_worker_is_never_enrolled_over_plain_http(self, tmp_path):
        with pytest.raises(ValueError, match="https"):
            main.enrol("linux-1", "hyperv-linux", "http://10.77.0.20:8443",
                       tls_dir=str(tmp_path))

    def test_every_cell_is_driven_through_an_agent(self):
        assert len(ar.TABLE) == 6
        assert set(ar.TABLE.values()) == {
            "control.agent_runtime:AgentRuntime"}

    def test_status_reads_the_store_and_says_what_is_there(self, tmp_path):
        from control.inventory import Inventory
        from store import schema
        from store.fleets import FleetStore
        db = str(tmp_path / "c.db")
        schema.init(db)
        FleetStore(db).seed({})
        Inventory(db).register_worker("linux-1", "hyperv-linux")
        main.capacity("github-linux-x64", 2, db=db)
        text = main.status(db=db)
        assert "linux-1" in text and "unknown" in text
        assert "github-linux-x64       capacity 2" in text
        assert "runners:" in text

    def test_capacity_on_an_unavailable_fleet_is_refused(self, tmp_path):
        from control.service import Refused
        from store import schema
        from store.fleets import FleetStore
        db = str(tmp_path / "c.db")
        schema.init(db)
        FleetStore(db).seed({})     # no Forgejo artefact for Windows
        with pytest.raises(Refused):
            main.capacity("forgejo-windows-x64", 1, db=db)

    def test_unit_images_come_from_the_deployment(self):
        assert main.unit_images({
            "RUNNER_UNIT_IMAGE_FORGEJO_WINDOWS": "forgejo-runner-win-v12",
            "RUNNER_UNIT_IMAGE_GITHUB_LINUX": " "}) == {
            ("forgejo", "windows"): "forgejo-runner-win-v12"}


class TestTheDeploymentsSettingsReachPlanning:
    """A cell can exist only because of a setting: Forgejo on Windows exists
    when FORGEJO_RUNNER_ARTIFACT_WINDOWS names a self-built runner. The
    reconciler plans without passing any settings, so the service has to
    hold them - found live, with the store recording the fleet available
    while every pass refused it as unbuildable."""

    ENV = {"FORGEJO_RUNNER_ARTIFACT_WINDOWS": "forgejo-runner-v13.1.0-windows",
           "FORGEJO_RUNNER_LABELS_WINDOWS": "rnr-pilot-windows:host"}

    def service(self, tmp_path, env):
        from control import agent_runtime
        from control.service import RunnerService
        from store import schema
        from store.fleets import FleetStore
        db = str(tmp_path / "c.db")
        schema.init(db)
        FleetStore(db).seed(env)
        return RunnerService(db, runtimes=agent_runtime.TABLE, env=env)

    def test_a_fleet_that_exists_only_through_them_can_be_planned(
            self, tmp_path):
        service = self.service(tmp_path, self.ENV)
        service.plan("forgejo-windows-x64", 1, requested_by="test")
        assert len(service.specs.list(fleet_id="forgejo-windows-x64")) == 1

    def test_without_them_it_is_still_refused(self, tmp_path):
        from control.service import Refused
        service = self.service(tmp_path, {})
        with pytest.raises(Refused, match="no windows runner binary"):
            service.plan("forgejo-windows-x64", 1, requested_by="test")

    def test_what_a_caller_passes_still_wins(self, tmp_path):
        from control.service import Refused
        service = self.service(tmp_path, self.ENV)
        with pytest.raises(Refused, match="no windows runner binary"):
            service.plan("forgejo-windows-x64", 1, requested_by="test", env={})
