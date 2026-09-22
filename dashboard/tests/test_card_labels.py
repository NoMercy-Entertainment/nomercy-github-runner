"""A card shows the labels its forge lists; a fleet the labels it registers with."""
import cards


def spec(**over):
    s = {"runner_id": "3f2504e0-4f89-41d3-9a0c-0305e82c3301", "display_name": "github-linux-x64-1",
         "provider": "github", "platform": "linux", "architecture": "x64",
         "host_id": "rnr-linux-1", "actual_state": "idle", "capabilities": {},
         "forge_labels": ["self-hosted", "Linux", "X64", "beast-unit"]}
    s.update(over)
    return s


def test_every_card_carries_labels():
    assert "labels" in cards.FIELDS


def test_a_card_shows_the_labels_the_forge_lists():
    assert cards.from_spec(spec())["labels"] == ["self-hosted", "Linux", "X64", "beast-unit"]


def test_labels_not_yet_seen_are_unknown_not_empty():
    assert cards.from_spec(spec(forge_labels=None))["labels"] is None


def test_a_registration_drift_note_is_a_warning():
    card = cards.from_spec(spec(last_note="2026-09-20T01:56:14Z registered with other labels than the fleet's: unexpected labels macos-13"))
    assert card["label_drift"].startswith("registered with other labels")
