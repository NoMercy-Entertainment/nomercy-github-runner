"""Wait for CBS write notifications; report terminal startup results only."""
import argparse
import ctypes
from ctypes import wintypes as w
from datetime import datetime
import hashlib
import http.client
import json
from pathlib import Path
import re
import subprocess
import tempfile

TASK = 'NoMercy-ServicingWatch-20261009'

def classify(line):
    if ' CBS ' not in line:
        return None
    if 'Failed a critical portion of startup processing.' in line:
        return 'failure'
    if re.search(r'Startup processing completed\..*HRESULT\s*=\s*0x0+\b', line):
        return 'cbs_complete'
    return None

class Changes:
    def __init__(self, directory):
        self.k = ctypes.WinDLL('kernel32', use_last_error=True)
        for name, arguments, result in (
            ('FindFirstChangeNotificationW', [w.LPCWSTR, w.BOOL, w.DWORD], w.HANDLE),
            ('FindNextChangeNotification', [w.HANDLE], w.BOOL),
            ('FindCloseChangeNotification', [w.HANDLE], w.BOOL),
            ('WaitForSingleObject', [w.HANDLE, w.DWORD], w.DWORD)):
            function = getattr(self.k, name)
            function.argtypes, function.restype = arguments, result
        self.handle = self.k.FindFirstChangeNotificationW(str(directory), False, 0x19)
        if not self.handle or self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    def wait(self, timeout=0xFFFFFFFF):
        result = self.k.WaitForSingleObject(self.handle, timeout)
        if result not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        if result == 0 and not self.k.FindNextChangeNotification(self.handle):
            raise ctypes.WinError(ctypes.get_last_error())
        return result == 0

    def close(self):
        self.k.FindCloseChangeNotification(self.handle)

def self_test():
    assert classify('2026-10-09 15:39:36, Info WinREAgent Successfully finished rollback') is None
    assert classify('2026-10-09 15:39:36, Info                  CSI    Completion status: S_OK') is None
    assert classify('2026-10-09 16:00:00, Info                  CBS    Startup processing completed. [HRESULT = 0x00000000]') == 'cbs_complete'
    assert classify('2026-10-09 16:00:00, Info                  CBS    Failed a critical portion of startup processing. [HRESULT = 0x800f0920]') == 'failure'
    assert classify('2026-10-09 16:00:00, Info                  CBS    Startup: Processing complete. [HRESULT = 0x80070bc2]') is None
    with tempfile.TemporaryDirectory(prefix='servicing-watch-test-') as directory:
        changes = Changes(directory)
        try:
            assert not changes.wait(30)
            (Path(directory) / 'CBS.log').write_text('synthetic notification test')
            assert changes.wait(5000), 'No file-change event received'
        finally:
            changes.close()
    print('PASS: real Windows notifications; sub-operation success is not recovery', flush=True)

def watch(config_path):
    config = json.loads(config_path.read_text())
    log = Path(r'C:\Windows\Logs\CBS\CBS.log')
    state_path = config_path.with_name('state.json')
    state = json.loads(state_path.read_text()) if state_path.exists() else {'sent': []}
    def send(kind, line=''):
        event = {'id': hashlib.sha256((kind + line).encode()).hexdigest(),
                 'kind': kind, 'at': datetime.now().astimezone().isoformat(),
                 'host_id': 'rnr-windows-1', 'detail': line[:800]}
        if event['id'] in state['sent']:
            return
        connection = http.client.HTTPConnection(config['host'], config['port'], timeout=20)
        try:
            connection.request('POST', '/event', json.dumps(event),
                               {'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json'})
            response = connection.getresponse()
            response.read()
            if response.status != 200:
                raise RuntimeError('Receiver rejected event: ' + str(response.status))
        finally:
            connection.close()
        state['sent'].append(event['id'])
        state_path.write_text(json.dumps(state))
    changes = Changes(log.parent)
    signature, offset, partial = None, 0, b''
    try:
        send('ready')
        initial = True
        while True:
            try:
                stat = log.stat()
                current = (stat.st_dev, stat.st_ino)
                if current != signature or stat.st_size < offset:
                    offset = max(0, stat.st_size - 2 * 1024 * 1024) if initial else 0
                    signature, partial = current, b''
                with log.open('rb') as stream:
                    stream.seek(offset)
                    data = stream.read()
                    offset = stream.tell()
                lines = (partial + data).split(b'\n')
                partial = lines.pop()
                for raw in lines:
                    line = raw.decode('utf-8', errors='replace').strip()
                    if line[:19] < config['created_local']:
                        continue
                    kind = classify(line)
                    if kind:
                        send(kind, line)
                        if kind == 'cbs_complete':
                            subprocess.run(['schtasks.exe', '/Delete', '/TN', TASK, '/F'],
                                           capture_output=True, timeout=20)
                            return
            except FileNotFoundError:
                pass  # Log rotation: wait for creation of the replacement.
            initial = False
            changes.wait()  # Kernel wait: no timer and no periodic log reads.
    finally:
        changes.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path)
    parser.add_argument('--self-test', action='store_true')
    arguments = parser.parse_args()
    if arguments.self_test:
        self_test()
    else:
        watch(arguments.config)
