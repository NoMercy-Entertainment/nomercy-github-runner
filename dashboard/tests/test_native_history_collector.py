"""The real collector avoids all log requests for an event-enabled worker."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


class CollectorStopped(BaseException):
    pass


@pytest.mark.parametrize("native", [True, False])
def test_collector_uses_native_wait_and_no_log_poll_for_upgraded_worker(tmp_path, monkeypatch, native):
    import app
    import dbwatch
    import history
    from store import schema
    path = str(tmp_path / "control.db")
    schema.init(path)
    monkeypatch.setattr(history, "DB_PATH", str(tmp_path / "history.db"))
    history.init()
    rid = "11111111-1111-4111-8111-111111111111"
    spec = {"runner_id": rid, "host_id": "worker-a", "provider": "github", "actual_state": "idle",
            "display_name": "native-test", "exec_unit_ref": "owned-unit", "telemetry": {}}
    calls, waits = [], []
    def fetch_logs(runner_id, since_seconds):
        calls.append((runner_id, since_seconds))
        return ""
    service = SimpleNamespace(specs=SimpleNamespace(path=path, list=lambda: [spec]),
                              inventory=SimpleNamespace(get=lambda _: {"capabilities": {"job_events": 1} if native else {}}),
                              fetch_logs=fetch_logs)
    monkeypatch.setattr(app.api_v2, "control_plane", lambda: (service, None))
    monkeypatch.setattr(history, "backfill_runner_ids", lambda _: None)
    class Watch:
        def __init__(self, watched):
            assert watched == path
        def wait(self, timeout=None):
            waits.append(timeout)
            raise CollectorStopped
        def close(self):
            pass
    monkeypatch.setattr(dbwatch, "DatabaseChanges", Watch)
    with pytest.raises(CollectorStopped):
        app._controller_collector()
    assert waits == [None if native else 5]
    assert calls == ([] if native else [(rid, 86400)])
