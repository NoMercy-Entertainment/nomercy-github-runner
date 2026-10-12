"""Startup .env settings preserve unrelated values and runner credentials."""
import importlib.util
from pathlib import Path
import os

import pytest
from agent import runner_environment


@pytest.fixture(params=['agent', 'image'])
def implementation(request):
    if request.param == 'agent':
        return runner_environment
    path = Path(__file__).resolve().parents[2] / 'images/linux/unit/runner/runner_environment.py'
    spec = importlib.util.spec_from_file_location('image_runner_environment', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_only_the_startup_setting_is_changed(implementation):
    before = ('PATH=/runner/bin\r\nOPAQUE_VALUE=keep=all=equals\r\n'
              'GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT=30\r\n'
              'GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT=60\r\n')
    after = implementation.channel_dotenv(before)
    assert after == ('PATH=/runner/bin\nOPAQUE_VALUE=keep=all=equals\n'
                     'GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT=300\n')
    assert implementation.channel_dotenv(after) == after


def test_preparation_keeps_registration_bytes_and_is_idempotent(tmp_path, implementation):
    path = tmp_path / '.env'
    registration = tmp_path / '.runner'
    registration.write_bytes(b'unchanged registration identity')
    path.write_text('OTHER=value\n')
    implementation.main(path)
    first = path.read_bytes()
    implementation.main(path)
    assert path.read_bytes() == first
    assert registration.read_bytes() == b'unchanged registration identity'
    assert not (tmp_path / '.env.channel-prepared').exists()


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX ownership and symlinks')
def test_environment_symlink_is_not_followed(tmp_path, implementation):
    outside = tmp_path / 'outside'
    outside.write_text('DO_NOT_TOUCH=1\n')
    path = tmp_path / '.env'
    path.symlink_to(outside)
    with pytest.raises(ValueError, match='owned regular file'):
        implementation.main(path)
    assert outside.read_text() == 'DO_NOT_TOUCH=1\n'


@pytest.mark.skipif(os.name != 'posix', reason='requires POSIX ownership')
def test_private_permissions_are_retained(tmp_path, implementation):
    path = tmp_path / '.env'
    path.write_text('PRIVATE=value\n')
    path.chmod(0o640)
    before = path.stat()
    implementation.main(path)
    after = path.stat()
    assert (after.st_uid, after.st_gid, after.st_mode & 0o777) == (before.st_uid, before.st_gid, 0o640)
