"""Receive guest events over loopback; wait for them using Linux inotify."""
import argparse
import ctypes
import hmac
import http.server
import json
import os
from pathlib import Path

def serve(directory):
    config = json.loads((directory / 'receiver.json').read_text())
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + config['token']):
                self.send_error(403)
                return
            length = int(self.headers.get('Content-Length', '0'))
            if self.path != '/event' or not 0 < length <= 4096:
                self.send_error(400)
                return
            event = json.loads(self.rfile.read(length))
            if event.get('host_id') != 'windows-arm64-1' or event.get('kind') not in ('ready', 'failure', 'cbs_complete'):
                self.send_error(400)
                return
            temporary = directory / 'event.tmp'
            with temporary.open('w') as stream:
                json.dump(event, stream)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o600)
            temporary.replace(directory / 'event.json')
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
    with http.server.HTTPServer(('127.0.0.1', config['port']), Handler) as server:
        while True:
            server.handle_request()  # Block in accept, without periodic status checks.

def wait(directory, kind, after):
    libc = ctypes.CDLL(None, use_errno=True)
    libc.inotify_init1.argtypes, libc.inotify_init1.restype = [ctypes.c_int], ctypes.c_int
    libc.inotify_add_watch.argtypes, libc.inotify_add_watch.restype = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32], ctypes.c_int
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'inotify_init1')
    try:
        if libc.inotify_add_watch(fd, os.fsencode(directory), 0x80) < 0:
            raise OSError(ctypes.get_errno(), 'inotify_add_watch')
        while True:
            path = directory / 'event.json'
            if path.exists():
                event = json.loads(path.read_text())
                if event['id'] != after and (event['kind'] == kind if kind else event['kind'] != 'ready'):
                    print(json.dumps(event), flush=True)
                    return
            os.read(fd, 4096)  # Block until an atomic event-file replacement.
    finally:
        os.close(fd)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('serve', 'wait'))
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--kind')
    parser.add_argument('--after', default='')
    args = parser.parse_args()
    serve(args.directory) if args.mode == 'serve' else wait(args.directory, args.kind, args.after)
