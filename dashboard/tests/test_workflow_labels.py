"""The labels a workflow can put in runs-on, as the forge has them."""
import providers as P

GH = P.by_key("github")
FJ = P.by_key("forgejo")


def test_github_record_labels_are_the_label_names():
    record = {"id": 7, "labels": [{"name": "self-hosted", "type": "read-only"},
                                  {"name": "Linux", "type": "read-only"},
                                  {"name": "beast-unit", "type": "custom"}]}
    assert GH.record_labels(record) == ["self-hosted", "Linux", "beast-unit"]


def test_forgejo_record_labels_keep_only_the_name():
    record = {"id": 3, "labels": ["ubuntu-latest", "ubuntu-22.04:docker://node:20"]}
    assert FJ.record_labels(record) == ["ubuntu-latest", "ubuntu-22.04"]


def test_forgejo_record_labels_as_objects():
    record = {"id": 3, "labels": [{"name": "windows-latest"}]}
    assert FJ.record_labels(record) == ["windows-latest"]


def test_a_record_without_labels_says_nothing():
    assert GH.record_labels({"id": 1}) is None
    assert FJ.record_labels(None) is None


def test_github_workflow_labels_add_what_github_gives_every_runner():
    fleet = {"platform": "linux", "architecture": "x64", "labels": []}
    assert GH.workflow_labels(fleet, {"RUNNER_LABELS": "beast-unit"}) == \
        ["self-hosted", "Linux", "X64", "beast-unit"]
    win = {"platform": "windows", "architecture": "x64", "labels": ["beast-unit"]}
    assert GH.workflow_labels(win, {}) == ["self-hosted", "Windows", "X64", "beast-unit"]


def test_forgejo_workflow_labels_are_the_names_of_the_fleet_labels():
    fleet = {"platform": "linux", "architecture": "x64",
             "labels": ["ubuntu-latest:docker://node:lts", "ubuntu-24.04:docker://node:22"]}
    assert FJ.workflow_labels(fleet, {}) == ["ubuntu-latest", "ubuntu-24.04"]


def test_workflow_labels_never_repeat_a_label():
    fleet = {"platform": "linux", "architecture": "x64",
             "labels": ["self-hosted", "Linux", "X64", "beast-unit"]}
    assert GH.workflow_labels(fleet, {}) == ["self-hosted", "Linux", "X64", "beast-unit"]
