import argparse
import glob
import json
import os
from pathlib import Path
import struct
import subprocess
import time

p = argparse.ArgumentParser()
p.add_argument('--binary', default='/opt/actions-runner-image/bin/Runner.Worker')
p.add_argument('--delay', type=float, default=35)
p.add_argument('--expect-received', action='store_true')
a = p.parse_args()
binary = Path(a.binary)
input_read, input_write = os.pipe()
output_read, output_write = os.pipe()
env = dict(os.environ, GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT='300')
started = time.monotonic()
worker = subprocess.Popen([str(binary), 'spawnclient', str(input_read), str(output_write)],
    pass_fds=(input_read, output_write), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
os.close(input_read)
os.close(output_write)
diag = binary.parent.parent / '_diag'
ready = False
deadline = started + 120
while time.monotonic() < deadline and worker.poll() is None:
    logs = sorted(diag.glob('Worker_*.log'))
    if logs and 'Waiting to receive the job message' in logs[-1].read_text(errors='replace'):
        ready = True
        break
    time.sleep(.1)
if not ready:
    if worker.poll() is None:
        worker.kill()
    stdout, stderr = worker.communicate(timeout=10)
    print('Worker failed to reach IPC receive', worker.returncode, stdout.decode(errors='replace')[:1500], stderr.decode(errors='replace')[:1500])
    print('DIAGNOSTIC FILES', list(diag.glob('*')))
    if logs:
        print(logs[-1].read_text(errors='replace')[-3000:])
    raise SystemExit(1)
time.sleep(a.delay)
# Deliberately invalid JSON: exercise the real transport without running a job.
payload = ('INVALID_PROBE_' + 'x' * 69000).encode('utf-16-le')
broken_pipe = False
try:
    with os.fdopen(input_write, 'wb') as stream:
        stream.write(struct.pack('<ii', 1, len(payload)))
        stream.write(payload)
except BrokenPipeError:
    broken_pipe = True
try:
    stdout, stderr = worker.communicate(timeout=25)
finally:
    if worker.poll() is None:
        worker.kill()
        worker.wait()
    os.close(output_read)
log = sorted(diag.glob('Worker_*.log'))[-1].read_text(errors='replace')
received = 'Message received.' in log
result = {'delay_seconds':a.delay,'elapsed_seconds':round(time.monotonic()-started,2),
          'channel_timeout_env':env['GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT'],
          'received':received,'receive_timed_out':'OperationCanceledException' in log,
          'broken_pipe':broken_pipe,'exit_code':worker.returncode}
print(json.dumps(result), flush=True)
assert received == a.expect_received, result
assert worker.returncode == 1, 'Invalid JSON must fail safely without starting a job'
