import plistlib
from types import SimpleNamespace

import pytest

from agent.runtimes.macos_storage import MacStorage

RID = 'c7343225-7446-4b87-87a6-cb516522e1dc'
ROOT = '/Users/runner/runners/' + RID
LIMIT = 100 * 1024 ** 3


@pytest.mark.parametrize('mount,quota,name,accepted', [
    (ROOT, LIMIT, 'rnr-' + RID, True),
    ('/System/Volumes/Data', LIMIT, 'rnr-' + RID, False),
    (ROOT, 0, 'rnr-' + RID, False),
    (ROOT, LIMIT, 'another-runner', False),
])
def test_quota_requires_an_observed_owned_mount(mount, quota, name, accepted):
    def run(args, timeout=30):
        if args[0] == '/bin/df':
            return True, 'Filesystem 512-blocks Used Available Capacity Mounted on\n/dev/disk2s7 10 2 8 20%% %s' % mount, ''
        return True, plistlib.dumps({'Containers': [{'Volumes': [{
            'DeviceIdentifier': 'disk2s7', 'Name': name,
            'CapacityQuota': quota, 'CapacityInUse': 2048}]}]}).decode(), ''
    storage = MacStorage(run, SimpleNamespace(), LIMIT, 'user')
    if accepted:
        assert storage.verify(RID)['CapacityQuota'] == LIMIT
    else:
        with pytest.raises(RuntimeError):
            storage.verify(RID)


def test_existing_plain_runner_tree_is_never_replaced():
    fs = SimpleNamespace(exists=lambda path: True)
    calls = []
    def run(args, timeout=30):
        calls.append(args)
        return False, '', 'not mounted'
    with pytest.raises(RuntimeError):
        MacStorage(run, fs, LIMIT, 'user').ensure(RID)
    assert all('addVolume' not in call for call in calls)


@pytest.mark.parametrize('existing', ['', '# unrelated mount\n',
    'UUID=DD742E76-024E-41B3-BBF8-F4046C43A364 ' + ROOT + ' apfs rw 0 0\n'])
def test_retry_finishes_mount_persistence_without_duplicating_a_volume(existing):
    files = {'/etc/fstab': existing}
    calls = []
    writes = []
    def write(path, text, mode=None):
        files[path] = text
        writes.append(path)
    fs = SimpleNamespace(exists=lambda p: p == ROOT or p in files,
                         read_text=lambda p: files[p], write_text=write,
                         remove=lambda p: files.pop(p))
    def run(args, timeout=30):
        calls.append(args)
        if '/usr/bin/install' in args:
            files['/etc/fstab'] = files[args[-2]]
        return True, '', ''
    storage = MacStorage(run, fs, LIMIT, 'runner')
    storage.verify = lambda rid: {'APFSVolumeUUID': 'DD742E76-024E-41B3-BBF8-F4046C43A364'}
    storage.ensure(RID)
    storage.ensure(RID)
    assert files['/etc/fstab'].count('UUID=') == 1
    assert len(writes) == (0 if 'UUID=' in existing else 1)
    assert all('addVolume' not in call for call in calls)
    assert existing.strip() in files['/etc/fstab']


@pytest.mark.parametrize('fstab', [
    'UUID=DD742E76-024E-41B3-BBF8-F4046C43A364 /another-runner apfs rw 0 0\n',
    'UUID=another-volume ' + ROOT + ' apfs rw 0 0\n',
])
def test_conflicting_persistent_mount_is_preserved(fstab):
    fs = SimpleNamespace(exists=lambda p: True, read_text=lambda p: fstab)
    storage = MacStorage(lambda *a, **kw: pytest.fail('conflict must not mutate the guest'),
                         fs, LIMIT, 'runner')
    storage.verify = lambda rid: {'APFSVolumeUUID': 'DD742E76-024E-41B3-BBF8-F4046C43A364'}
    with pytest.raises(RuntimeError, match='conflicts'):
        storage.ensure(RID)
