"""Which of the six provider x platform cells exist, answered in data.

The point of this file is that no caller anywhere should know anything about
Windows or macOS. It asks `supports()` and renders what comes back. So every
cell of the matrix is asserted here, once, with the reason it gives - because
an unavailable fleet and a broken one look identical if the answer is a bare
False.

The asymmetry between the two forges is real and is the reason the axis exists
at all. GitHub documents self-hosted runners on Linux, Windows and macOS.
Forgejo publishes twelve release assets and all twelve are Linux; its Makefile
carries a DARWIN_ARCHS variable that no release target consumes, which is dead
configuration inherited upstream rather than evidence of a build. So the
Forgejo Windows and macOS cells exist only when someone has built the binary
and said where it is.
"""
import ast
import os

import providers as P


class TestTheMatrix:
    """One assertion per cell. Six cells, six tests, no loops - a loop that
    drifted would hide which cell changed."""

    def test_github_linux(self):
        assert P.GITHUB.supports(P.LINUX, P.X64, {})

    def test_github_windows(self):
        """Documented: Windows 10/11 and Server 2016/2019/2022, 64-bit."""
        assert P.GITHUB.supports(P.WINDOWS, P.X64, {})

    def test_github_macos(self):
        """Documented: macOS 11.0 or later, x64 and ARM64."""
        assert P.GITHUB.supports(P.MACOS, P.X64, {})
        assert P.GITHUB.supports(P.MACOS, P.ARM64, {})

    def test_forgejo_linux(self):
        assert P.FORGEJO.supports(P.LINUX, P.X64, {})
        assert P.FORGEJO.supports(P.LINUX, P.ARM64, {})

    def test_forgejo_windows_is_unavailable_until_someone_builds_it(self):
        s = P.FORGEJO.supports(P.WINDOWS, P.X64, {})
        assert not s
        assert "publishes no windows runner binary" in s.reason
        assert "FORGEJO_RUNNER_ARTIFACT_WINDOWS" in s.reason, (
            "the reason must say how to enable it, or an operator has to read "
            "the source to find out")

    def test_forgejo_macos_is_unavailable_until_someone_builds_it(self):
        s = P.FORGEJO.supports(P.MACOS, P.X64, {})
        assert not s
        assert "FORGEJO_RUNNER_ARTIFACT_MACOS" in s.reason


class TestConfiguringTheSelfBuiltArtefact:
    def test_naming_the_artefact_enables_the_cell(self):
        env = {"FORGEJO_RUNNER_ARTIFACT_MACOS": "forgejo-runner-darwin-arm64"}
        assert P.FORGEJO.supports(P.MACOS, P.ARM64, env)

    def test_the_artefact_is_marked_self_built(self):
        """Provenance is not cosmetic: a self-built binary has no upstream
        release feed, and this fleet has already lost a day to a pinned runner
        version being deprecated with no warning."""
        env = {"FORGEJO_RUNNER_ARTIFACT_MACOS": "forgejo-runner-darwin-arm64"}
        a = P.FORGEJO.agent_artifact(P.MACOS, P.ARM64, env)
        assert a.source == "self-built"
        assert a.reference == "forgejo-runner-darwin-arm64"
        assert "no upstream release feed" in a.notes

    def test_a_vendor_artefact_says_so(self):
        a = P.FORGEJO.agent_artifact(P.LINUX, P.X64, {})
        assert a.source == "vendor"

    def test_an_unsupported_cell_has_no_artefact(self):
        assert P.FORGEJO.agent_artifact(P.WINDOWS, P.X64, {}) is None

    def test_whitespace_does_not_count_as_configured(self):
        env = {"FORGEJO_RUNNER_ARTIFACT_WINDOWS": "   "}
        assert not P.FORGEJO.supports(P.WINDOWS, P.X64, env)


class TestUnknownInputIsAnswered:
    def test_an_unknown_platform_does_not_raise(self):
        """A caller passing rubbish gets a reason, not a traceback: this is
        reached from an HTTP route."""
        for prov in P.ALL:
            s = prov.supports("plan9", P.X64, {})
            assert not s and "unknown platform" in s.reason

    def test_support_is_falsy_but_carries_its_reason(self):
        s = P.FORGEJO.supports(P.WINDOWS, P.X64, {})
        assert bool(s) is False
        assert s.reason


class TestDeregistrationDiffersAndTheDifferenceMatters:
    def test_github_deregisters_itself(self):
        d = P.GITHUB.deregistration({"registration_id": "7"})
        assert d.via_api is False
        assert "SIGTERM" in d.note

    def test_forgejo_can_only_be_removed_through_the_api(self):
        """forgejo-runner has no unregister subcommand, so a container stopped
        any other way strands its record."""
        d = P.FORGEJO.deregistration({"registration_id": "9",
                                      "registration_uuid": "u"})
        assert d.via_api is True
        assert d.registration_uuid == "u"
        assert "refuse the removal" in d.note


class TestTheSeamStaysClosed:
    def _imports(self, path):
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        return names

    def test_a_provider_never_learns_how_a_runner_is_executed(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        imported = self._imports(os.path.join(here, "providers.py"))
        assert "runtime" not in imported, imported
        assert "docker_ops" not in imported, imported

    def test_the_token_field_names_are_declared_for_redaction(self):
        """A token added later without an entry here is exactly the failure
        redaction exists to prevent, so the list lives beside what produces
        them."""
        assert "FORGEJO_RUNNER_REGISTRATION_TOKEN" in P.REDACTED_FIELDS
        assert "GH_TOKEN" in P.REDACTED_FIELDS
        assert "registration_token" in P.REDACTED_FIELDS
