"""Exercise the deployed dashboard boundary: no local engine, remote agents."""
from datetime import datetime, timedelta, timezone

import pytest

import api_v2
import history
from control.secrets import SecretStore
from store import schema
from store.fleets import FleetStore


@pytest.fixture
def plane(tmp_path, monkeypatch):
    path = str(tmp_path / "control.db")
    schema.init(path)
    FleetStore(path).seed({})
    monkeypatch.setattr(api_v2, "_db_path", lambda: path)
    monkeypatch.setattr(schema, "DB_PATH", path)
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "history.db"))
    history.init()
    now = datetime.now(timezone.utc).isoformat()
    with schema.connect(path) as c:
        c.execute("INSERT INTO controller_status VALUES(1,?,'running',NULL)", (now,))
    return path


@pytest.mark.parametrize("platform", ["linux", "windows", "macos"])
def test_reads_reach_the_runner_worker_on_every_platform(client, plane, monkeypatch, platform):
    calls = []
    def call(self, host, verb, body, **kwargs):
        calls.append((host, verb, body))
        return {"exec_unit.logs": {"text": "remote log"},
                "exec_unit.status": {"exists": True, "running": True},
                "exec_unit.telemetry": {"cpu_percent": 27, "mem_used_bytes": 1234}}[verb]
    monkeypatch.setattr(api_v2._LazyAgentClient, "call_and_wait", call)
    import docker_ops
    monkeypatch.setattr(docker_ops, "logs_since", lambda *a: pytest.fail("local engine called"))
    service, note = api_v2.control_plane()
    service.inventory.register_worker('worker-one', 'hyperv-linux')
    rid = service.specs.create(provider="github", platform=platform,
                               host_id="worker-one", exec_unit_ref="existing-unit",
                               fleet_id=f"github-{platform}-x64")
    assert client.get(f"/api/v2/runners/{rid}/logs").json["result"] == "remote log"
    assert client.get(f"/api/v2/runners/{rid}/status").json["result"]["observed"]["running"]
    assert client.get(f"/api/v2/runners/{rid}/resources").json["result"]["cpu_percent"] == 27
    assert all(host == "worker-one" and body["runner_id"] == rid for host, _, body in calls)


def test_liveness_expires_without_a_heartbeat(client, plane):
    assert client.get('/api/v2/fleet').json['control_plane']['running']
    old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    with schema.connect(plane) as c:
        c.execute("UPDATE controller_status SET last_seen_at=?", (old,))
    data = client.get('/api/v2/fleet').json
    assert not data['control_plane']['running']
    assert all(not a['enabled'] for f in data['fleets'] for a in f['actions'])


def test_maintenance_blocks_runner_changes_but_allows_defaults(client, plane):
    assert client.post('/api/v2/maintenance', json={'enabled': True}).status_code == 200
    response = client.post('/api/v2/fleets/github-linux-x64/capacity',
                           json={'desired': 3}, headers={'Idempotency-Key': 'capacity'})
    assert response.status_code == 409
    assert client.post('/api/v2/settings/github-linux-x64', json={
        'memory_limit': 8 * 1024**3, 'cpu_limit': '4', 'labels': ['linux', 'build'],
        'unit_template': 'runner:v2'}).status_code == 200
    service, _ = api_v2.control_plane()
    assert service.fleets.get('github-linux-x64')['memory_limit'] == 8 * 1024**3
    assert service.unit_image(service.fleets.get('github-linux-x64')) == 'runner:v2'
    assert service.fleets.get('github-linux-x64')['desired_capacity'] == 0


@pytest.mark.parametrize('value', [-1, True, '8g', 0])
def test_invalid_memory_limit_cannot_poison_defaults(client, plane, value):
    res = client.post('/api/v2/settings/github-linux-x64', json={'memory_limit': value})
    assert res.status_code == 400
    assert FleetStore(plane).get('github-linux-x64')['memory_limit'] is None


def test_token_changes_reach_new_api_services_without_restart(plane, monkeypatch):
    monkeypatch.setenv('GH_TOKEN', 'old-deployment-token')
    SecretStore(plane).set('GH_TOKEN', 'new-stored-token', 'admin')
    service, _ = api_v2.control_plane()
    assert service.env['GH_TOKEN'] == 'new-stored-token'


def test_agent_logs_store_identity_and_do_not_close_a_newer_job_on_replay(plane):
    spec = {'runner_id': 'stable-uuid', 'provider': 'github', 'display_name': 'runner-one',
            'actual_state': 'busy', 'telemetry': {'cpu_percent': 22, 'mem_used_bytes': 4096,
                                                 'at': '2026-09-21T11:00:05Z'}}
    first = '2026-09-21 10:00:00Z: Running job: build\n2026-09-21 10:01:00Z: Job build completed with result: Succeeded'
    history.record_controller_logs(spec, first)
    history.record_controller_logs(spec, '2026-09-21 11:00:00Z: Running job: build', '2026-09-21T11:00:05Z')
    history.record_controller_logs(spec, first, '2026-09-21T11:00:05Z')
    runs = history.list_runs(runner_id='stable-uuid')
    assert len(runs) == 2
    assert runs[0]['ended_at'] is None
    assert runs[1]['result'] == 'Succeeded'
    assert len(history.get_run(runs[0]['id'])['samples_data']) == 1


def test_controller_settings_page_has_six_fleets_and_never_env_editor(client, plane, monkeypatch):
    import app
    monkeypatch.setattr(app.ops, 'engine_reachable', lambda: False)
    assert b'Defaults for every fleet' in client.get('/settings').data
    assert len(client.get('/api/v2/settings').json['fleets']) == 6
    assert client.post('/api/settings', json={'GH_TOKEN': 'secret'}).status_code == 409
    assert client.post('/api/runner/start', json={'name': 'old-runner'}).status_code == 409


def test_replayed_completion_does_not_close_an_older_job_with_a_missing_end(plane):
    spec = {'runner_id': 'stable-uuid', 'provider': 'github', 'display_name': 'runner-one'}
    history.record_controller_logs(spec, '2026-09-21 10:00:00Z: Running job: build')
    completed = ('2026-09-21 11:00:00Z: Running job: build\n'
                 '2026-09-21 11:01:00Z: Job build completed with result: Succeeded')
    history.record_controller_logs(spec, completed)
    before = history.list_runs(runner_id='stable-uuid')
    history.record_controller_logs(spec, completed)
    assert history.list_runs(runner_id='stable-uuid') == before
    assert before[0]['ended_at'] == '2026-09-21T11:01:00Z'
    assert before[1]['ended_at'] is None


def test_swap_ceiling_is_linux_only_and_preserves_physical_memory(client, plane):
    endpoint = '/api/v2/settings/github-linux-x64'
    assert client.post(endpoint, json={'memory_limit': 32 * 1024**3,
                                       'memory_swap_limit': 48 * 1024**3}).status_code == 200
    fleet = FleetStore(plane).get('github-linux-x64')
    assert fleet['memory_limit'] == 32 * 1024**3
    assert fleet['memory_swap_limit'] == 48 * 1024**3
    assert client.post(endpoint, json={'memory_swap_limit': 16 * 1024**3}).status_code == 400
    assert client.post(endpoint, json={'memory_limit': 64 * 1024**3}).status_code == 400
    assert client.post('/api/v2/settings/github-windows-x64',
                       json={'memory_swap_limit': 48 * 1024**3}).status_code == 400
    assert client.post(endpoint, json={'memory_swap_limit': None}).status_code == 200
    assert FleetStore(plane).get('github-linux-x64')['memory_limit'] == 32 * 1024**3


def test_disk_limit_is_refused_until_a_matching_worker_declares_enforcement(client, plane):
    endpoint = '/api/v2/settings/github-linux-x64'
    assert client.post(endpoint, json={'disk_limit': 20 * 1024**3}).status_code == 400
    assert client.post(endpoint, json={'disk_limit': None}).status_code == 200
    rows = client.get('/api/v2/settings').json['fleets']
    assert not next(f for f in rows if f['fleet_id'] == 'github-linux-x64')['disk_quota_supported']
    service, _ = api_v2.control_plane()
    caps = {'kind': 'linux-container', 'architecture': 'x64', 'disk_quota': True}
    service.inventory.register_worker('quota-worker', 'hyperv-linux', capabilities=caps)
    service.inventory.heartbeat('quota-worker', capabilities=caps)
    assert client.post(endpoint, json={'disk_limit': 20 * 1024**3}).status_code == 200


def test_controller_history_uses_uuid_across_renames_and_shared_display_names(tmp_path, monkeypatch):
    import history
    monkeypatch.setattr(history, 'DB_PATH', str(tmp_path / 'history.db'))
    history.init()
    logs = ('2026-09-21 10:00:00Z: Running job: compile\n'
            '2026-09-21 10:01:00Z: Job compile completed with result: Succeeded')
    one = {'runner_id': 'uuid-one', 'display_name': 'first', 'provider': 'github'}
    history.record_controller_logs(one, logs)
    history.record_controller_logs(dict(one, display_name='renamed'), logs)
    assert len(history.list_runs(runner_id='uuid-one')) == 1
    two = dict(one, runner_id='uuid-two')
    history.record_controller_logs(two, logs)
    assert len(history.list_runs(runner_id='uuid-two')) == 1
    assert history.list_runs(runner_id='uuid-two')[0]['ended_at'] == '2026-09-21T10:01:00Z'
    assert history.list_runs(runner_id='uuid-one')[0]['id'] != history.list_runs(runner_id='uuid-two')[0]['id']


def test_cache_policy_can_disable_cleanup_and_request_drain_first(client, plane):
    policy = {'max_bytes': 20 * 1024**3, 'scopes': ['engine-build-cache'],
              'enabled': False, 'on_clear': 'drain-first'}
    endpoint = '/api/v2/settings/github-linux-x64'
    assert client.post(endpoint, json={'cache_policy': policy}).status_code == 200
    assert FleetStore(plane).get('github-linux-x64')['cache_policy'] == policy
    for malformed in ({'enabled': 'false'}, {'on_clear': 'kill'}, {'scopes': ['host-root']}):
        assert client.post(endpoint, json={'cache_policy': malformed}).status_code == 400
    assert FleetStore(plane).get('github-linux-x64')['cache_policy'] == policy
