"""What differs between the two forges, in one place.

The dashboard controls runners for GitHub Actions and for Forgejo. Almost
everything about them is the same - containers on one engine, each with its own
nested Docker daemon - so this module holds only what genuinely differs, and
docker_ops stays a single code path.

Deliberately data and construction, never behaviour that touches Docker:
docker_ops imports this module, so anything here calling back into it would be
a circular import. The forge API clients are safe to build here because neither
of them imports docker_ops.
"""

import os
import re

LABEL_PROVIDER = "nomercy.provider"

_NAME_RE = re.compile(r"(?:github|forgejo)-runner-\d+")


class Provider:
    def __init__(self, key, prefix, image, registration_path,
                 registration_key):
        self.key = key
        self.prefix = prefix
        self.image = image
        self.registration_path = registration_path
        # The field inside the runner's registration file that holds the name
        # the forge knows it by. GitHub writes "agentName"; Forgejo "name".
        self.registration_key = registration_key

    def __repr__(self):
        return f"<Provider {self.key}>"

    def name_for(self, index):
        return f"{self.prefix}{index}"

    def container_env(self, env, name=None):
        """Environment for a new runner container, as (dict, error).

        Returns an error rather than raising because one of the two has to
        talk to the network to build it - Forgejo mints a fresh registration
        token per runner - and a failed create must render, not 500.

        `name` is the container name create() is about to use
        (provider.name_for(index)). GitHub does not need it - the agent name
        is whatever actions-runner picks at registration. Forgejo does: it is
        what the runner registers as, and without it a dashboard-created
        runner registers under its container ID instead of a name matching
        what `docker ps` and Forgejo's own runner list both show.
        """
        raise NotImplementedError

    def forge_client(self, env):
        """An API client, or None when the deployment is not configured."""
        raise NotImplementedError


class _GitHub(Provider):
    def container_env(self, env, name=None):
        return {
            "GH_TOKEN": env.get("GH_TOKEN", ""),
            "GITHUB_ORG": env.get("GITHUB_ORG", "NoMercy-Entertainment"),
            "RUNNER_LABELS": env.get("RUNNER_LABELS", "self-hosted,Linux,X64"),
            "RUNNER_GROUP": env.get("RUNNER_GROUP", ""),
        }, None

    def forge_client(self, env):
        import github_api
        token, org = env.get("GH_TOKEN"), env.get("GITHUB_ORG")
        if not (token and org):
            return None
        return github_api.GitHub(token, org)


class _Forgejo(Provider):
    def container_env(self, env, name=None):
        url = (env.get("FORGEJO_INSTANCE_URL") or "").strip()
        if not url:
            return {}, "FORGEJO_INSTANCE_URL is not set"
        # Checked like the URL and the token, and BEFORE a registration token
        # is minted, so a create that cannot succeed does not burn one.
        # start-forgejo.sh passes this straight to `register --labels`, and an
        # empty value there is not an error: the runner registers, shows as
        # idle in Forgejo and in this dashboard, and silently never picks up a
        # job, because a job is matched to a runner by label. That failure has
        # no symptom to search for, which is why it is refused here instead.
        labels = (env.get("FORGEJO_RUNNER_LABELS") or "").strip()
        if not labels:
            return {}, ("FORGEJO_RUNNER_LABELS is not set - a runner with no "
                        "labels never picks up a job")
        client = self.forge_client(env)
        if client is None:
            return {}, "FORGEJO_ADMIN_TOKEN is not set"
        token = client.registration_token()
        if not token:
            return {}, ("could not mint a registration token - check "
                        "FORGEJO_ADMIN_TOKEN and that Forgejo is reachable")
        return {
            "FORGEJO_INSTANCE_URL": url,
            "FORGEJO_RUNNER_REGISTRATION_TOKEN": token,
            "FORGEJO_RUNNER_LABELS": labels,
            # Without this, scripts/start-forgejo.sh falls back to
            # $(hostname), which Docker sets to the container ID - not
            # --name - so a dashboard-created runner would register with
            # Forgejo under a hex string instead of forgejo-runner-<N>,
            # unlike the statically-declared forgejo-runner-1 in
            # docker-compose.runners.yml. Matching still works either way
            # (docker_ops keys runners on uuid), but an operator comparing
            # Forgejo's runner list against `docker ps` needs the names to
            # agree.
            "FORGEJO_RUNNER_NAME": name or "",
        }, None

    def forge_client(self, env):
        import forgejo_api
        url = (env.get("FORGEJO_INSTANCE_URL") or "").strip()
        token = (env.get("FORGEJO_ADMIN_TOKEN") or "").strip()
        if not (url and token):
            return None
        return forgejo_api.Forgejo(url, token)


GITHUB = _GitHub(
    key="github",
    prefix="github-runner-",
    image=os.environ.get(
        "RUNNER_IMAGE",
        "ghcr.io/nomercy-entertainment/nomercy-github-runner:latest"),
    registration_path="/root/actions-runner/.runner",
    registration_key="agentName",
)

FORGEJO = _Forgejo(
    key="forgejo",
    prefix="forgejo-runner-",
    image=os.environ.get(
        "FORGEJO_RUNNER_IMAGE",
        "ghcr.io/nomercy-entertainment/nomercy-forgejo-runner:latest"),
    registration_path="/data/.runner",
    registration_key="name",
)

ALL = (GITHUB, FORGEJO)
_BY_KEY = {p.key: p for p in ALL}


def by_key(key):
    return _BY_KEY.get((key or "").strip().lower())


def for_name(name):
    for p in ALL:
        if (name or "").startswith(p.prefix):
            return p
    return None


def from_label(label_value, name):
    """The provider of a container. The label decides; the name is the
    fallback for containers created before the label existed - which is every
    runner currently deployed."""
    return by_key(label_value) or for_name(name)


def valid_name(name):
    return bool(_NAME_RE.fullmatch(name or ""))
