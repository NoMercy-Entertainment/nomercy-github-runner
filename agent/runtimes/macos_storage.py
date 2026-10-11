"""Owned APFS runner volumes with observed quotas and persistent mount points."""
import plistlib
import posixpath
import re
from .. import naming


class MacStorage:
    def __init__(self, run, fs, limit, user):
        self._run, self._fs, self._storage_limit, self._user = run, fs, limit, user

    @staticmethod
    def _check(result):
        ok, out, err = result
        if not ok:
            raise RuntimeError(err or out or 'APFS operation failed')

    def ensure(self, runner_id):
        root = posixpath.dirname(naming.names(naming.check(runner_id), 'macos')['work'])
        if self._fs.exists(root):
            owned = self.verify(runner_id)
            self._persist(owned, root, runner_id)
            return owned
        ok, out, err = self._run(['/usr/sbin/diskutil', 'apfs', 'list', '-plist'], timeout=30)
        if not ok:
            raise RuntimeError('APFS containers cannot be inspected')
        containers = [c['ContainerReference'] for c in plistlib.loads(out.encode())['Containers']
                      if any('System' in v.get('Roles', []) for v in c['Volumes'])]
        if len(containers) != 1 or not re.fullmatch(r'disk[0-9]+', containers[0]):
            raise RuntimeError('APFS system container is ambiguous')
        self._fs.makedirs(posixpath.dirname(root))
        self._fs.makedirs(root)
        self._check(self._run(['/usr/bin/sudo', '-n', '/usr/sbin/diskutil', 'apfs', 'addVolume',
                               containers[0], 'APFS', 'rnr-' + runner_id,
                               '-mountpoint', root, '-quota', str(self._storage_limit)], timeout=120))
        owned = self.verify(runner_id)
        self._persist(owned, root, runner_id)
        return owned

    def _persist(self, owned, root, runner_id):
        """Finish a partially completed provision without creating another volume."""
        volume_uuid = owned.get('APFSVolumeUUID')
        if not isinstance(volume_uuid, str) or not re.fullmatch(
                r'[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}', volume_uuid):
            raise RuntimeError('runner APFS volume UUID is missing or invalid')
        fstab = self._fs.read_text('/etc/fstab') if self._fs.exists('/etc/fstab') else ''
        line = 'UUID=%s %s apfs rw 0 0' % (volume_uuid, root)
        present = False
        for existing in fstab.splitlines():
            fields = existing.split('#', 1)[0].split()
            if len(fields) >= 2 and (fields[0].lower() == ('UUID=' + volume_uuid).lower()
                                     or fields[1] == root):
                if fields != line.split():
                    raise RuntimeError('runner APFS persistent mount conflicts with /etc/fstab')
                present = True
        self._check(self._run(['/usr/bin/sudo', '-n', '/usr/sbin/chown',
                               self._user, root], timeout=30))
        if present:
            return
        temporary = '/tmp/rnr-fstab-' + runner_id
        self._fs.write_text(temporary, fstab.rstrip('\n') + '\n' + line + '\n', mode=0o600)
        try:
            self._check(self._run(['/usr/bin/sudo', '-n', '/usr/bin/install', '-o', 'root',
                                   '-g', 'wheel', '-m', '644', temporary, '/etc/fstab'], timeout=30))
            if line not in self._fs.read_text('/etc/fstab').splitlines():
                raise RuntimeError('runner APFS persistent mount was not saved')
        finally:
            self._fs.remove(temporary)

    def verify(self, runner_id):
        """Observe the UUID-named APFS volume and its actual quota, never infer it."""
        root = posixpath.dirname(naming.names(naming.check(runner_id), 'macos')['work'])
        ok, out, err = self._run(['/bin/df', '-P', root], timeout=30)
        rows = out.splitlines()
        if not ok or len(rows) != 2 or rows[1].split()[-1] != root:
            raise RuntimeError('runner APFS volume is not mounted at its owned root')
        device = rows[1].split()[0].removeprefix('/dev/')
        ok, out, err = self._run(['/usr/sbin/diskutil', 'apfs', 'list', '-plist'], timeout=30)
        if not ok:
            raise RuntimeError('runner APFS quota cannot be inspected')
        volumes = [v for c in plistlib.loads(out.encode())['Containers'] for v in c['Volumes']
                   if v.get('DeviceIdentifier') == device]
        if (len(volumes) != 1 or volumes[0].get('Name') != 'rnr-' + runner_id
                or volumes[0].get('CapacityQuota') != self._storage_limit):
            raise RuntimeError('runner APFS volume identity or quota differs')
        return volumes[0]
