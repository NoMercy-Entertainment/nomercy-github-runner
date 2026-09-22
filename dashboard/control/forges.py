"""The forges, as the provisioning flow sees them: records, deleting one, and
draining a runner whose forge is where it is drained.

The flow's `Forges` protocol, over the providers' own adapters. The flow never
holds a forge client: it asks here, and this asks the provider for the
runner's cell, with the deployment's environment. That keeps the tokens in one
place, and it is the one door a forge record is deleted through (T-1002):
forgejo-runner has no `unregister`, so for Forgejo this is not one way of
removing a record but the only one.

**Unknown is not empty, and not deleted is not deleted.** `records` answers
None when the forge could not be asked, never [] - the flow reads None as
"unknown" and never as "idle". `delete` answers True only when the forge
confirmed the record gone, and a transport failure is raised rather than
turned into False, so the reason reaches the runner's `last_error`.
"""
from typing import Mapping, Optional


class ForgeUnreachable(RuntimeError):
    """The forge did not answer a delete. The removal waiting on it must be
    refused, not forced: forcing it strands the record."""


class LiveForges:
    def __init__(self, env: Optional[Mapping[str, str]] = None):
        self.env = dict(env or {})

    def records(self, provider) -> Optional[list]:
        try:
            return provider.forge_records(self.env)
        except Exception:               # noqa: BLE001 - unknown, not empty
            return None

    def delete(self, provider, registration_id) -> bool:
        if not registration_id:
            raise ValueError("no registration id: nothing identifies the "
                             "record to delete")
        try:
            deleted = provider.delete_record(self.env, str(registration_id))
        except Exception as e:          # noqa: BLE001
            raise ForgeUnreachable(
                f"{provider.key}: the forge could not be asked to delete "
                f"registration {registration_id}: {type(e).__name__}") from e
        if not deleted:
            raise ForgeUnreachable(
                f"{provider.key}: the forge did not confirm registration "
                f"{registration_id} deleted")
        return True

    def drain(self, provider, spec) -> None:
        """Stop the forge giving this runner jobs (OPEN-7). The provider
        raises when the forge did not confirm it; that reaches `last_error`
        as it is, and the drain is asked again on the next pass."""
        client = None
        group = None
        if provider.key == "github":
            group = (self.env.get("GITHUB_DRAIN_GROUP") or "").strip()
            client = provider.forge_client(self.env)
            if not group or client is None or not client.drain_group_closed(group):
                raise ForgeUnreachable(
                    "GitHub drain requires GITHUB_DRAIN_GROUP with verified "
                    "selected visibility and zero repositories; removing "
                    "custom labels does not prevent self-hosted jobs")
        provider.drain_at_forge(self.env, spec)
        if client is not None and not client.drain_group_closed(
                group, spec.get("registration_id")):
            raise ForgeUnreachable(
                "GitHub did not confirm runner membership in the closed "
                "drain group; destructive actions remain blocked")

    def cancel_drain(self, provider, spec) -> None:
        """Let the forge give this runner jobs again."""
        provider.undrain_at_forge(self.env, spec)
