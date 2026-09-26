"""Real browser acceptance using temporary stores and a fake worker transport."""
import os
import threading
from datetime import datetime, timezone

import pytest


def test_controller_pages_use_the_same_state_and_show_remote_history(client, tmp_path, monkeypatch):
    playwright = pytest.importorskip('playwright.sync_api')
    from werkzeug.serving import make_server
    import app
    import api_v2
    import history
    from store import schema
    from store.fleets import FleetStore
    path = str(tmp_path / 'control.db')
    schema.init(path)
    FleetStore(path).seed({})
    monkeypatch.setattr(schema, 'DB_PATH', path)
    monkeypatch.setattr(api_v2, '_db_path', lambda: path)
    monkeypatch.setattr(history, 'DB_PATH', str(tmp_path / 'history.db'))
    monkeypatch.setattr(api_v2._LazyAgentClient, 'call_and_wait',
                        lambda *args, **kwargs: {'text': 'Verified remote worker log'})
    history.init()
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    with schema.connect(path) as c:
        c.execute("INSERT INTO controller_status VALUES(1,?,'running',NULL)", (now,))
    service, _ = api_v2.control_plane()
    caps = {'kind': 'linux-container', 'builds_from': 'image', 'max_instances': 20}
    service.inventory.register_worker('worker', 'hyperv-linux', capabilities=caps)
    service.inventory.heartbeat('worker', capabilities=caps)
    rid = service.specs.create(provider='github', platform='linux', fleet_id='github-linux-x64',
                               host_id='worker', display_name='test-runner', actual_state='idle',
                               exec_unit_ref='test-unit')
    history.open_run('test-runner', 'test-runner', 'compile', '2026-09-20T10:00:00Z', runner_id=rid)
    history.add_sample('test-runner', 20, 1024**3, '2026-09-20T10:00:02Z')
    history.add_sample('test-runner', 40, 2*1024**3, '2026-09-20T10:00:04Z')
    history.close_run('test-runner', 'compile', '2026-09-20T10:00:06Z', 'Succeeded')
    server = make_server('127.0.0.1', 0, app.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    errors = []
    try:
        with playwright.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={'width': 1400, 'height': 1100})
            context.add_cookies([{'name': 'session', 'value': client.get_cookie('session').value,
                                 'url': f'http://127.0.0.1:{server.server_port}'}])
            page = context.new_page()
            frames = []
            page.on('websocket', lambda socket: socket.on('framereceived', lambda frame: frames.append(frame)))
            page.on('pageerror', lambda error: errors.append(str(error)))
            base = f'http://127.0.0.1:{server.server_port}'
            page.goto(base + '/')
            playwright.expect(page.locator('section.fleet')).to_have_count(6)
            playwright.expect(page.locator('#plane')).to_have_text('Control plane running')
            page.wait_for_timeout(200)
            assert any('snapshot' in str(frame) for frame in frames)
            fleet = page.locator('section[data-fleet="github-linux-x64"]')
            playwright.expect(fleet.get_by_role('button', name='Set capacity')).to_have_count(0)
            fleet.get_by_role('button', name='Add runner').click()
            playwright.expect(fleet.locator('.fcount')).to_contain_text('1 runner')
            assert FleetStore(path).get('github-linux-x64')['desired_capacity'] == 1
            page.goto(base + '/settings')
            playwright.expect(page.locator('form[data-fleet]')).to_have_count(6)
            playwright.expect(page.locator('[name="memory_swap_limit"]')).to_have_count(2)
            form = page.locator('form[data-fleet="github-linux-x64"]')
            form.locator('[name="memory_limit"]').fill('32')
            form.locator('[name="memory_swap_limit"]').fill('48')
            form.locator('[name="labels"]').fill('linux, build')
            playwright.expect(form.locator('[name="disk_limit"]')).to_be_disabled()
            form.locator('[name="cache_max_bytes"]').fill('20')
            form.locator('[name="cache_enabled"]').uncheck()
            form.locator('[name="cache_on_clear"]').select_option('drain-first')
            form.locator('[name="cache_scopes"]').select_option('engine-build-cache')
            form.get_by_role('button', name='Save defaults').click()
            playwright.expect(page.locator('#message')).to_contain_text('Saved')
            assert FleetStore(path).get('github-linux-x64')['memory_limit'] == 32 * 1024**3
            assert FleetStore(path).get('github-linux-x64')['memory_swap_limit'] == 48 * 1024**3
            assert FleetStore(path).get('github-linux-x64')['cache_policy'] == {
                'max_bytes': 20 * 1024**3, 'enabled': False, 'on_clear': 'drain-first',
                'scopes': ['engine-build-cache']}
            page.locator('#GH_TOKEN').fill('browser-token-sentinel')
            page.locator('form[data-secret="GH_TOKEN"]').get_by_role('button').click()
            playwright.expect(page.locator('#GH_TOKEN')).to_have_value('')
            assert 'browser-token-sentinel' not in page.locator('body').inner_text()
            page.goto(base + '/runners/' + rid)
            page.get_by_role('button', name='compile', exact=True).click()
            playwright.expect(page.locator('#graphs svg')).to_have_count(2)
            page.get_by_role('button', name='Load the last 5 minutes').click()
            playwright.expect(page.locator('#logs')).to_have_text('Verified remote worker log')
            output = os.environ.get('RUNNER_UI_SCREENSHOT')
            if output:
                page.screenshot(path=output, full_page=True)
            assert not errors, errors
            context.close()
            browser.close()
    finally:
        server.shutdown()
