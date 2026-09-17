"""Every name declared secret is actually masked, everywhere it can be read.

`providers.REDACTED_FIELDS` described itself as "consumed by the central
redaction helper" and was consumed by nothing. `runner_detail` kept its own
hand-written pair of names instead, so `FORGEJO_RUNNER_REGISTRATION_TOKEN` -
which docker-compose.runners.yml puts into every Forgejo runner's environment,
and which mints further runner registrations at the instance - was rendered in
full on the runner detail page to anyone who could log in.

A declared list that nothing reads redacts nothing. These tests are what make
the declaration load-bearing: the list is now the single source, and a token
added to it is masked without anyone remembering to update a second set.
"""
import providers
import runner_detail


class TestTheDeclaredListIsTheOneThatIsUsed:
    def test_every_declared_field_is_masked(self):
        """The property the whole arrangement rests on."""
        missing = sorted(providers.REDACTED_FIELDS - set(
            runner_detail.SECRET_KEYS))
        assert missing == [], (
            "declared secret but not masked; a list nothing reads redacts "
            "nothing")

    def test_the_two_known_tokens_are_still_covered(self):
        """The original pair must not be lost while widening the set."""
        assert "GH_TOKEN" in runner_detail.SECRET_KEYS
        assert "FORGEJO_API_TOKEN" in runner_detail.SECRET_KEYS


class TestTheForgejoRegistrationTokenSpecifically:
    """The live leak this file was written for."""

    def test_it_is_masked_in_a_containers_environment(self):
        env = runner_detail._mask_env([
            "FORGEJO_RUNNER_REGISTRATION_TOKEN=abcd1234efgh5678",
            "FORGEJO_INSTANCE_URL=https://git.example",
        ])
        assert env["FORGEJO_RUNNER_REGISTRATION_TOKEN"] != "abcd1234efgh5678"
        assert "1234efgh" not in env["FORGEJO_RUNNER_REGISTRATION_TOKEN"]

    def test_a_non_secret_value_is_left_readable(self):
        """Masking everything would make the page useless."""
        env = runner_detail._mask_env(["FORGEJO_INSTANCE_URL=https://git.x"])
        assert env["FORGEJO_INSTANCE_URL"] == "https://git.x"

    def test_a_short_token_reveals_nothing_at_all(self):
        env = runner_detail._mask_env(["GH_TOKEN=short"])
        assert set(env["GH_TOKEN"]) == {"*"}


class TestTheSourceOfTruth:
    def test_runner_detail_does_not_keep_its_own_list(self):
        """Two hand-maintained sets are how the leak happened. One set, or the
        next token added to providers goes unmasked the same way."""
        with open(runner_detail.__file__, encoding="utf-8") as fh:
            source = fh.read()
        assert "REDACTED_FIELDS" in source, (
            "SECRET_KEYS must be derived from the declared list, not retyped")
