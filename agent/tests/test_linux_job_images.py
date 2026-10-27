"""A Forgejo unit keeps the images its jobs run in, and fetches them itself.

Forgejo Linux jobs run in the image each label names, inside the unit's own
nested engine. That image was once loaded into each engine by hand, and the
unit's own startup cleanup (`docker image prune -af`) or a fresh volume took
it away again: the next job failed "pull access denied" (2026-10-27).

`/runner/job_images.py` closes both holes: every image the unit's labels name
is held by a stopped pin container, which no prune removes, and an image that
is not there is pulled from the worker's job-image registry before a job asks
for it. The images come from the registration's own label list, so a recreate
with new labels lets go of the old images by itself.
"""
import importlib.util
import json
from pathlib import Path

import pytest

UNIT = Path(__file__).parents[2] / "images" / "linux" / "unit"
SCRIPT = UNIT / "runner" / "job_images.py"
spec = importlib.util.spec_from_file_location("unit_job_images", SCRIPT)
job_images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job_images)

REGISTRY = "172.17.0.1:5000"
NOBLE = f"{REGISTRY}/nomercy/github-runner-base:tools-20261027.2"
JAMMY = f"{REGISTRY}/nomercy/job-ubuntu-22.04:20261027.2"


def runner_file(tmp_path, labels):
    path = tmp_path / ".runner"
    path.write_text(json.dumps({"id": 7, "uuid": "u", "token": "secret",
                                "labels": labels}))
    return path


class FakeEngine:
    """Just enough of a nested engine: images by reference and containers."""

    def __init__(self, images=(), pins=None, pullable=()):
        self.images = {ref: f"sha256:{i:064x}" for i, ref in enumerate(images, 1)}
        self.pullable = set(pullable)
        self.containers = dict(pins or {})   # name -> (ref label, image id)
        self.calls = []

    def __call__(self, *args, timeout=120):
        self.calls.append(args)
        if args[:2] == ("image", "inspect"):
            ref = args[-1]
            if ref in self.images:
                return True, self.images[ref]
            return False, "No such image"
        if args[0] == "pull":
            ref = args[-1]
            if ref not in self.pullable:
                return False, "pull access denied"
            self.images[ref] = f"sha256:{len(self.images) + 100:064x}"
            return True, ""
        if args[0] == "ps":
            return True, "\n".join(f"{name}\t{ref}" for name, (ref, _)
                                   in sorted(self.containers.items()))
        if args[0] == "inspect":
            name = args[-1]
            if name in self.containers:
                return True, self.containers[name][1]
            return False, "No such object"
        if args[0] == "rm":
            self.containers.pop(args[-1], None)
            return True, ""
        if args[0] == "create":
            name = args[args.index("--name") + 1]
            ref = args[-1]
            self.containers[name] = (ref, self.images[ref])
            return True, name
        raise AssertionError(f"unexpected docker call {args}")


class TestWhichImages:
    def test_the_docker_labels_name_the_images(self, tmp_path):
        path = runner_file(tmp_path, [
            f"ubuntu-latest:docker://{NOBLE}",
            f"ubuntu-22.04:docker://{JAMMY}",
            f"ubuntu-24.04:docker://{NOBLE}",
            "self-hosted:host",
            "plain"])
        assert job_images.wanted(path) == sorted([JAMMY, NOBLE])

    def test_a_unit_not_registered_yet_wants_nothing(self, tmp_path):
        assert job_images.wanted(tmp_path / ".runner") == []

    def test_a_label_with_options_names_only_the_image(self, tmp_path):
        """Forgejo allows `docker://image` only; anything after the image
        reference is not part of it."""
        path = runner_file(tmp_path, [f"ubuntu-latest:docker://{NOBLE}"])
        assert job_images.wanted(path) == [NOBLE]


class TestPinning:
    def test_a_present_image_gets_a_pin_no_prune_removes(self):
        engine = FakeEngine(images=[NOBLE])
        job_images.sync([NOBLE], engine, pull=False)
        (name, (ref, image)), = engine.containers.items()
        assert ref == NOBLE and image == engine.images[NOBLE]
        create = next(c for c in engine.calls if c[0] == "create")
        # Never started: a pin holds the image and nothing else.
        assert "--label" in create and f"{job_images.PIN_LABEL}={NOBLE}" in create
        assert "--network" in create and "none" in create

    def test_a_pin_is_made_once(self):
        engine = FakeEngine(images=[NOBLE])
        job_images.sync([NOBLE], engine, pull=False)
        job_images.sync([NOBLE], engine, pull=False)
        assert sum(1 for c in engine.calls if c[0] == "create") == 1

    def test_a_pin_for_an_image_no_label_names_is_removed(self):
        engine = FakeEngine(images=[NOBLE, "old/image:1"])
        job_images.sync(["old/image:1"], engine, pull=False)
        job_images.sync([NOBLE], engine, pull=False)
        assert [ref for ref, _ in engine.containers.values()] == [NOBLE]

    def test_a_pin_holding_an_older_build_of_the_tag_is_replaced(self):
        engine = FakeEngine(images=[NOBLE])
        job_images.sync([NOBLE], engine, pull=False)
        engine.images[NOBLE] = "sha256:" + "f" * 64     # the tag moved
        job_images.sync([NOBLE], engine, pull=False)
        (ref, image), = engine.containers.values()
        assert image == "sha256:" + "f" * 64

    def test_without_pull_a_missing_image_is_left_for_later(self):
        engine = FakeEngine(pullable=[NOBLE])
        job_images.sync([NOBLE], engine, pull=False)
        assert not any(c[0] == "pull" for c in engine.calls)
        assert engine.containers == {}


class TestFetching:
    def test_a_missing_image_is_pulled_and_pinned(self):
        engine = FakeEngine(pullable=[NOBLE, JAMMY])
        assert job_images.sync([JAMMY, NOBLE], engine, pull=True) == []
        assert sorted(ref for ref, _ in engine.containers.values()) == sorted([JAMMY, NOBLE])

    def test_a_failed_pull_is_reported_and_the_rest_still_done(self):
        engine = FakeEngine(pullable=[NOBLE])
        failed = job_images.sync([JAMMY, NOBLE], engine, pull=True)
        assert failed == [JAMMY]
        assert [ref for ref, _ in engine.containers.values()] == [NOBLE]

    def test_a_present_image_is_not_pulled_again(self):
        engine = FakeEngine(images=[NOBLE])
        job_images.sync([NOBLE], engine, pull=True)
        assert not any(c[0] == "pull" for c in engine.calls)


class TestTheUnitUsesIt:
    def read(self, *parts):
        return (UNIT.joinpath(*parts)).read_text(encoding="utf-8")

    def test_pins_are_made_before_the_startup_prune(self):
        run = self.read("runner", "run")
        pin = run.index("job_images.py pin")
        assert pin < run.index("/runner/cleanup\n")

    def test_missing_images_are_fetched_once_the_runner_is_up(self):
        run = self.read("runner", "run")
        assert "job_images.py pull" in run
        assert run.index("job_images.py pull") > run.index('"$FORGEJO_RUNNER_BIN" daemon')

    def test_the_nested_engine_may_pull_from_the_workers_registry(self):
        run = self.read("runner", "run")
        assert '"insecure-registries"' in run
        assert "RUNNER_JOB_IMAGE_REGISTRY" in run
        assert f"RUNNER_JOB_IMAGE_REGISTRY={REGISTRY}" in self.read("Dockerfile.forgejo")

    def test_a_registry_name_cannot_break_the_engine_config(self):
        """The list is written into daemon.json: only host[:port] names."""
        run = self.read("runner", "run")
        assert "[A-Za-z0-9.-]" in run

    def test_the_forgejo_image_ships_python_for_it(self):
        assert "python3" in self.read("Dockerfile.forgejo")
