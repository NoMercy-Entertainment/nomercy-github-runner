"""The images this Forgejo unit's jobs run in: kept, and fetched before a job asks.

Forgejo Linux jobs run in the image each `name:docker://image` label names,
inside this unit's nested engine. Two things used to take that image away:
the startup cleanup (`docker image prune -af`), and a unit made on a fresh
engine volume. The next job then failed "pull access denied" (2026-10-27).

- `pin`: every image the registration's labels name, if present, is held by a
  stopped container that is never started. `docker image prune -a` leaves an
  image a container uses, so neither the startup cleanup nor the dashboard's
  "unused images" clear can remove it. Pins for images no label names any
  more are removed, so a recreate with new labels lets the old images go.
- `pull`: the same, after pulling each named image that is not here. The
  worker's job-image registry serves the images no public registry has.

Only the forge's registration file is read; nothing here needs a token.
"""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PIN_LABEL = "nomercy.job_image"
PIN_PREFIX = "nomercy-job-image-"
#: A 20 GB image from the worker's registry, on a busy disk.
PULL_TIMEOUT = 3600


def wanted(runner_file):
    """The images the registration's labels name, sorted and unique."""
    try:
        labels = json.loads(Path(runner_file).read_text(encoding="utf-8")).get("labels") or []
    except (OSError, ValueError, AttributeError):
        return []
    images = set()
    for label in labels:
        _, _, rest = str(label).partition(":")
        if rest.startswith("docker://"):
            image = rest[len("docker://"):].split()[0] if rest[len("docker://"):].strip() else ""
            if image:
                images.add(image)
    return sorted(images)


def pin_name(ref):
    return PIN_PREFIX + hashlib.sha256(ref.encode()).hexdigest()[:16]


def docker(*args, timeout=120):
    try:
        done = subprocess.run(["docker", *args], capture_output=True, text=True,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s"
    return done.returncode == 0, (done.stdout if done.returncode == 0 else done.stderr).strip()


def _image_id(run, ref):
    ok, out = run("image", "inspect", "--format", "{{.Id}}", ref)
    return out.strip() if ok else None


def sync(refs, run=docker, pull=False):
    """Pin each of `refs` that is present - pulling it first when `pull` -
    and drop every other pin. Returns the references that are still missing."""
    refs = list(refs)
    ok, out = run("ps", "-a", "--filter", f"label={PIN_LABEL}",
                  "--format", '{{.Names}}\t{{.Label "' + PIN_LABEL + '"}}')
    pins = {}
    for line in (out.splitlines() if ok else []):
        name, _, ref = line.partition("\t")
        if name:
            pins[name] = ref
    for name, ref in pins.items():
        if ref not in refs:
            run("rm", "-f", name)
    missing = []
    for ref in refs:
        image = _image_id(run, ref)
        if image is None and pull:
            print(f"job image {ref}: pulling", flush=True)
            done, err = run("pull", "-q", ref, timeout=PULL_TIMEOUT)
            if not done:
                print(f"job image {ref}: pull failed: {err}", file=sys.stderr, flush=True)
            image = _image_id(run, ref)
        if image is None:
            missing.append(ref)
            continue
        name = pin_name(ref)
        if pins.get(name) == ref:
            # The image the pin holds, by id: a tag pushed again names a new
            # build, and the pin is moved onto it.
            done, held = run("inspect", "--format", "{{.Image}}", name)
            if done and held.strip() == image:
                continue
        if name in pins:
            run("rm", "-f", name)
        run("create", "--name", name, "--label", f"{PIN_LABEL}={ref}",
            "--network", "none", "--entrypoint", "true", ref)
        print(f"job image {ref}: kept", flush=True)
    return missing


def main(argv):
    mode = argv[1] if len(argv) > 1 else "pin"
    if mode not in ("pin", "pull"):
        print("usage: job_images.py pin|pull", file=sys.stderr)
        return 64
    reg = os.environ.get("RUNNER_REG_DIR", "/runner/reg")
    missing = sync(wanted(Path(reg) / ".runner"), pull=mode == "pull")
    if missing and mode == "pull":
        print("job images still missing: " + " ".join(missing), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
