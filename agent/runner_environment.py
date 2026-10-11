"""Uniform GitHub Listener/Worker startup settings, independent of job limits."""
from pathlib import Path
import os
import sys

CHANNEL_TIMEOUT_KEY = 'GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT'
CHANNEL_TIMEOUT_SECONDS = '300'


def channel_dotenv(text):
    # Runner.Listener loads .env after inheriting its service environment.
    # Preserve every other entry, including credentials that must not be logged.
    lines = [line for line in text.splitlines() if line.partition('=')[0] != CHANNEL_TIMEOUT_KEY]
    return '\n'.join(lines + [CHANNEL_TIMEOUT_KEY + '=' + CHANNEL_TIMEOUT_SECONDS]) + '\n'


def main(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Runner environment must be an owned regular file')
    text = path.read_text(encoding='utf-8') if path.exists() else ''
    changed = channel_dotenv(text)
    if changed == text:
        return
    temporary = path.with_name(path.name + '.channel-prepared')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(changed)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() and os.name == 'posix':
            existing = path.stat()
            os.chown(temporary, existing.st_uid, existing.st_gid)
            os.chmod(temporary, existing.st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__ == '__main__':
    main(sys.argv[1])
