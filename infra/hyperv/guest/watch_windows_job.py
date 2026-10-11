"""Wait for one GitHub runner job to finish using Windows file notifications."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent))
from watch_windows_servicing import Changes


def completion(line, job, after):
    match = re.fullmatch(r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})Z: Job (.+) completed with result: (\w+)', line)
    if match and match[2] == job and match[1] >= after:
        return {'kind': 'job_complete', 'job': job, 'result': match[3],
                'at': match[1].replace(' ', 'T') + 'Z'}
    return None


def watch(runner, job, after):
    runner = str(UUID(runner))
    datetime.strptime(after, '%Y-%m-%d %H:%M:%S')
    log = Path(r'D:\runners') / runner / 'logs' / 'runner.log'
    changes = Changes(log.parent)
    identity, offset, partial = None, 0, b''
    try:
        print(json.dumps({'kind': 'watch_ready', 'runner_id': runner}), flush=True)
        initial = True
        while True:
            try:
                stat = log.stat()
                current = (stat.st_dev, stat.st_ino)
                if current != identity or stat.st_size < offset:
                    identity, partial = current, b''
                    offset = max(0, stat.st_size - 2 * 1024 * 1024) if initial else 0
                with log.open('rb') as stream:
                    stream.seek(offset)
                    lines = (partial + stream.read()).split(b'\n')
                    offset = stream.tell()
                partial = lines.pop()
                for raw in lines:
                    event = completion(raw.decode('utf-8', errors='replace').strip(), job, after)
                    if event:
                        event['runner_id'] = runner
                        event['received_at'] = datetime.now(timezone.utc).isoformat()
                        print(json.dumps(event), flush=True)
                        return
            except FileNotFoundError:
                pass
            initial = False
            changes.wait()
    finally:
        changes.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--runner', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--after', required=True, help='UTC timestamp of the target job start')
    args = parser.parse_args()
    watch(args.runner, args.job, args.after)
