"""The hosted tool cache: in the images, and in the GitHub runner's tool cache.

setup-python and setup-node look in RUNNER_TOOL_CACHE before they download.
A Forgejo job's container has it at /opt/hostedtoolcache (forgejo-runner sets
that for docker jobs), a GitHub unit at <work>/_tool on its own volume. Python
3.10-3.14 and Node 20/22/24/26 are put into /opt/hostedtoolcache when the
images are built (images/linux/unit/toolcache/), from the very builds the
setup actions would download, checked against their published hashes; the
GitHub unit copies what is missing into its own tool cache when it starts
(images/linux/unit/runner/toolcache.py).

Run 989 of FiLL/deepseek-bridge spent its setup-python step downloading
3.12.16 into an empty cache (2026-10-27).
"""
import importlib.util
import re
from pathlib import Path

import pytest

UNIT = Path(__file__).parents[2] / "images" / "linux" / "unit"
SCRIPT = UNIT / "runner" / "toolcache.py"
spec = importlib.util.spec_from_file_location("unit_toolcache", SCRIPT)
toolcache = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toolcache)

VERSIONS = UNIT / "toolcache" / "versions.txt"


def entries():
    rows = []
    for line in VERSIONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            rows.append(line.split())
    return rows


class TestTheVersions:
    @pytest.mark.parametrize("os_version", ["22.04", "24.04"])
    def test_python_310_to_314_for_each_ubuntu(self, os_version):
        found = {row[1].rsplit(".", 1)[0] for row in entries()
                 if row[0] == "python" and row[2] == os_version}
        assert found == {"3.10", "3.11", "3.12", "3.13", "3.14"}

    def test_node_20_22_24_26(self):
        found = {row[1].split(".")[0] for row in entries() if row[0] == "node"}
        assert found == {"20", "22", "24", "26"}

    def test_every_build_is_the_setup_actions_own_and_hashed(self):
        for kind, version, os_version, url, digest in entries():
            repo = {"python": "python-versions", "node": "node-versions"}[kind]
            assert url.startswith(f"https://github.com/actions/{repo}/releases/download/{version}-"), url
            assert re.fullmatch(r"sha256:[0-9a-f]{64}", digest), digest
            if kind == "python":
                assert url.endswith(f"-linux-{os_version}-x64.tar.gz")

    def test_the_installer_checks_the_hash_before_it_runs_anything(self):
        install = (UNIT / "toolcache" / "install.sh").read_text(encoding="utf-8")
        assert install.index("sha256sum -c") < install.index("./setup.sh")
        assert "x64.complete" in install


class TestTheImagesCarryIt:
    def test_the_noble_job_image_has_the_2404_builds(self):
        tools = (UNIT / "Dockerfile.tools").read_text(encoding="utf-8")
        assert "toolcache/install.sh 24.04 /opt/hostedtoolcache" in tools

    def test_the_jammy_job_image_has_the_2204_builds(self):
        jammy = (UNIT / "Dockerfile.jammy").read_text(encoding="utf-8")
        assert re.search(r"catthehacker/ubuntu:act-22.04@sha256:[0-9a-f]{64}", jammy)
        assert "toolcache/install.sh 22.04 /opt/hostedtoolcache" in jammy

    def test_the_github_unit_seeds_its_own_tool_cache(self):
        run = (UNIT / "runner" / "run").read_text(encoding="utf-8")
        assert 'toolcache.py /opt/hostedtoolcache' in run
        assert '"$RUNNER_WORK_DIR/_tool"' in run


def version_dir(root, tool, version, complete=True, payload="bin"):
    d = root / tool / version
    (d / "x64").mkdir(parents=True)
    (d / "x64" / payload).write_text(version)
    if complete:
        (d / "x64.complete").write_text("")
    return d


class TestSeeding:
    def test_missing_versions_are_copied_complete(self, tmp_path):
        src, dst = tmp_path / "image", tmp_path / "work" / "_tool"
        version_dir(src, "Python", "3.12.16")
        version_dir(src, "node", "22.23.4")
        copied = toolcache.seed(src, dst)
        assert sorted(copied) == ["Python/3.12.16", "node/22.23.4"]
        assert (dst / "Python" / "3.12.16" / "x64.complete").exists()
        assert (dst / "node" / "22.23.4" / "x64" / "bin").read_text() == "22.23.4"

    def test_a_complete_version_is_left_alone(self, tmp_path):
        src, dst = tmp_path / "image", tmp_path / "work" / "_tool"
        version_dir(src, "Python", "3.12.16")
        mine = version_dir(dst, "Python", "3.12.16", payload="pip-installed")
        assert toolcache.seed(src, dst) == []
        assert (mine / "x64" / "pip-installed").exists()

    def test_a_half_download_is_replaced(self, tmp_path):
        src, dst = tmp_path / "image", tmp_path / "work" / "_tool"
        version_dir(src, "Python", "3.12.16")
        version_dir(dst, "Python", "3.12.16", complete=False, payload="partial")
        assert toolcache.seed(src, dst) == ["Python/3.12.16"]
        assert not (dst / "Python" / "3.12.16" / "x64" / "partial").exists()
        assert (dst / "Python" / "3.12.16" / "x64.complete").exists()

    def test_an_incomplete_image_version_is_not_copied(self, tmp_path):
        src, dst = tmp_path / "image", tmp_path / "work" / "_tool"
        version_dir(src, "Python", "3.12.16", complete=False)
        assert toolcache.seed(src, dst) == []

    def test_versions_a_job_added_stay(self, tmp_path):
        src, dst = tmp_path / "image", tmp_path / "work" / "_tool"
        version_dir(src, "Python", "3.12.16")
        theirs = version_dir(dst, "Python", "3.9.25")
        toolcache.seed(src, dst)
        assert (theirs / "x64.complete").exists()

    def test_links_in_the_tool_cache_are_never_followed(self, tmp_path):
        src, dst = tmp_path / "image", tmp_path / "work" / "_tool"
        version_dir(src, "Python", "3.12.16")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        dst.mkdir(parents=True)
        try:
            (dst / "Python").symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            pytest.skip("no symlinks here")
        assert toolcache.seed(src, dst) == []
        assert list(elsewhere.iterdir()) == []

    def test_no_image_cache_is_nothing_to_do(self, tmp_path):
        assert toolcache.seed(tmp_path / "none", tmp_path / "work" / "_tool") == []
