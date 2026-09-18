"""Per-instance storage names, and why they are a security control.

`clear_cache` deletes what a runner owns. For that to be safe, "owns" has to be
something a machine can decide, and these names are the mechanism: every path a
runner can be told to clear is generated from its own id, so a scope naming a
shared location cannot be built in the first place.

The tests below are therefore mostly about what CANNOT be produced. Two ids
never colliding is what keeps one runner's clear from touching another's work.
A malformed id being refused is what stops a name escaping its own directory.
And there being no display-name parameter at all is what stops two runners that
happen to share a name from sharing their storage.
"""
import inspect
import uuid

import pytest

import providers
from store import storage
from store.storage import InvalidRunnerId

ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
OTHER = "550e8400-e29b-41d4-a716-446655440000"

PLATFORMS = (providers.LINUX, providers.WINDOWS, providers.MACOS)


class TestTheFiveAreas:
    def test_all_five_are_named(self):
        assert set(storage.names(ID)) == {"work", "docker", "cache", "reg",
                                          "logs"}

    def test_linux_gets_volume_names(self):
        n = storage.names(ID, providers.LINUX)
        assert n["work"] == f"rnr-{ID}-work"
        assert n["docker"] == f"rnr-{ID}-docker"

    def test_windows_gets_paths_under_one_directory(self):
        n = storage.names(ID, providers.WINDOWS)
        assert n["work"].endswith(f"\\{ID}\\work")
        assert n["cache"].startswith(storage.WINDOWS_ROOT)

    def test_macos_gets_guest_local_paths(self):
        n = storage.names(ID, providers.MACOS)
        assert n["work"] == f"{storage.MACOS_ROOT}/{ID}/work"

    def test_an_area_that_cannot_exist_is_none_not_a_guess(self):
        """A Windows runner has no nested engine and the appliance runs no
        engine at all. Inventing a name would produce storage that is asked
        for, never found, and reported as an error with no cause."""
        assert storage.names(ID, providers.WINDOWS)["docker"] is None
        assert storage.names(ID, providers.MACOS)["docker"] is None

    def test_linux_is_the_one_platform_with_a_nested_engine(self):
        assert storage.names(ID, providers.LINUX)["docker"] is not None

    def test_an_unknown_platform_is_refused(self):
        with pytest.raises(ValueError, match="unknown platform"):
            storage.names(ID, "plan9")


class TestDeterminism:
    def test_the_same_id_always_gives_the_same_names(self):
        """A name that moved between calls would orphan the volume holding the
        data, silently, while everything appeared to work."""
        for platform in PLATFORMS:
            assert storage.names(ID, platform) == storage.names(ID, platform)

    def test_an_id_in_a_different_case_is_the_same_runner(self):
        assert storage.names(ID.upper()) == storage.names(ID.lower())


class TestCollisionFreedom:
    def test_two_ids_share_no_name(self):
        for platform in PLATFORMS:
            a = {v for v in storage.names(ID, platform).values() if v}
            b = {v for v in storage.names(OTHER, platform).values() if v}
            assert a & b == set(), platform

    def test_no_two_ids_collide_across_many(self):
        """The property clear_cache leans on: one runner's clear can never
        reach another's data."""
        seen = set()
        for _ in range(200):
            for name in storage.names(str(uuid.uuid4())).values():
                assert name not in seen
                seen.add(name)

    def test_two_areas_of_one_runner_are_distinct(self):
        n = storage.names(ID)
        assert len(set(n.values())) == len(n)


class TestNothingIsDerivedFromADisplayName:
    def test_the_function_takes_no_name(self):
        """The structural guarantee. Two runners may share a display name, so
        storage keyed on one would be storage two runners share - and a clear
        on either would delete the other's work."""
        parameters = set(inspect.signature(storage.names).parameters)
        assert parameters == {"runner_id", "platform"}

    def test_a_display_name_never_appears_in_a_name(self):
        for name in storage.names(ID).values():
            assert "github-runner" not in (name or "")


class TestAMalformedIdCannotBecomeAPath:
    """The escape this validation exists to prevent."""

    @pytest.mark.parametrize("bad", [
        "../../var/lib/docker",
        "..",
        "/etc/passwd",
        r"..\..\Windows",
        "github-runner-1",
        "",
        "3f2504e0-4f89-41d3-9a0c-0305e82c3301/../x",
        None,
        12345,
    ])
    def test_it_is_refused(self, bad):
        with pytest.raises(InvalidRunnerId):
            storage.names(bad)

    def test_the_refusal_reaches_every_entry_point(self):
        for fn in (storage.names, storage.root):
            with pytest.raises(InvalidRunnerId):
                fn("../escape")

    def test_a_real_uuid_is_accepted(self):
        assert storage.check(str(uuid.uuid4()))

    def test_it_is_refused_rather_than_cleaned_up(self):
        """A sanitised id raises the question of whether it still refers to
        the same runner. The answer to a malformed identity is no identity."""
        with pytest.raises(InvalidRunnerId):
            storage.check("3f2504e0-4f89-41d3-9a0c-0305e82c3301 ")


class TestContainment:
    def test_every_name_sits_under_the_runners_own_root(self):
        for platform in PLATFORMS:
            prefix = storage.root(ID, platform)
            for area, name in storage.names(ID, platform).items():
                if name is None:
                    continue
                assert name.startswith(prefix), (platform, area, name)

    def test_one_runners_root_does_not_contain_anothers_name(self):
        for platform in PLATFORMS:
            prefix = storage.root(ID, platform)
            for name in storage.names(OTHER, platform).values():
                assert not (name or "").startswith(prefix)

    def test_ownership_is_decided_by_construction(self):
        n = storage.names(ID)
        assert storage.owns(ID, n["cache"])
        assert not storage.owns(OTHER, n["cache"])

    def test_a_shared_location_is_never_owned(self):
        """The rule from the design: a scope that cannot be attributed is not
        offered. These are the sort of names that must never pass."""
        for shared in ("/var/lib/docker", "buildkit", "rnr-shared-cache",
                       "D:\\runners", "", None):
            assert not storage.owns(ID, shared)

    def test_a_near_miss_is_not_owned(self):
        """Prefix-matching alone would accept this; membership does not."""
        assert not storage.owns(ID, f"rnr-{ID}-work-backup")
