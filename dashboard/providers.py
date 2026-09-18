"""What differs between the two forges, in one place.

The dashboard controls runners for GitHub Actions and for Forgejo. Almost
everything about them is the same - containers on one engine, each with its own
nested Docker daemon - so this module holds only what genuinely differs, and
docker_ops stays a single code path.

Deliberately data and construction, never behaviour that touches Docker:
docker_ops imports this module, so anything here calling back into it would be
a circular import. The forge API clients are safe to build here because neither
of them imports docker_ops.

Since the platform axis was added, this module answers a second question as
well: which of the six provider x platform combinations actually exist, and on
what terms. That answer is DATA, not a conditional at a call site - a caller
asks `supports()` and renders the reason it gets back, rather than knowing
anything itself about Windows or macOS. It is also honest about provenance:
Forgejo publishes no Windows or macOS runner binary, so those cells are
available only when a self-built artefact has been configured, and say so when
it has not.

Nothing here imports a runtime module. A provider must never learn how a runner
is executed, exactly as a runtime must never learn which forge it belongs to;
tests/test_provider_platforms.py asserts both directions.
"""

import os
import re
from dataclasses import dataclass

LABEL_PROVIDER = "nomercy.provider"

_NAME_RE = re.compile(r"(?:github|forgejo)-runner-\d+")

#: The platforms a runner instance can run on.
LINUX, WINDOWS, MACOS = "linux", "windows", "macos"
PLATFORMS = (LINUX, WINDOWS, MACOS)

#: Architectures. Kept separate from platform because they vary independently
#: and the forges answer for them separately.
X64, ARM64 = "x64", "arm64"

#: Field names whose VALUES must never reach a log, an API response or an audit
#: record. This is the single source: `runner_detail.SECRET_KEYS` is derived
#: from it, so adding a name here is what makes it masked. Listed in this
#: module because this is the module that produces these values, and a token
#: added later without an entry here is the failure mode redaction exists to
#: prevent.
REDACTED_FIELDS = frozenset({
    "registration_token",
    # RegistrationPlan's own field. Missed in T-0003, which listed
    # "registration_token" - a name nothing uses - while the dataclass carrying
    # the credential calls it `token`, so asdict(plan) would have gone out
    # unmasked. A test now builds a plan and checks every field that holds its
    # token is on this list, rather than trusting two spellings to agree.
    "token",
    "GH_TOKEN",
    "FORGEJO_API_TOKEN",
    "FORGEJO_RUNNER_REGISTRATION_TOKEN",
    # Design 18.3 names it; it was missing until T-1901.
    "OIDC_CLIENT_SECRET",
})


#: The four answers `job_state` gives, and the only four.
BUSY, IDLE, OFFLINE, UNKNOWN = "busy", "idle", "offline", "unknown"


def _labels(spec, default):
    """A spec's labels as the comma list a registration takes.

    The fleet's labels when it has them, else the deployment default. Never
    empty in practice for Forgejo - that is refused before a token is minted -
    because a runner with no labels registers, looks healthy, and never picks
    up a job.
    """
    labels = (spec or {}).get("labels")
    if isinstance(labels, (list, tuple)) and labels:
        return ",".join(str(x) for x in labels)
    if isinstance(labels, str) and labels.strip():
        return labels.strip()
    return (default or "").strip()


#: forgejo-runner's executor types. A label is `<name>:<type>://<image>` for
#: docker and lxc, and `<name>:host` for host; a bare `<name>` is host too, to
#: the runner (https://forgejo.org/docs/latest/admin/actions/configuration/).
FORGEJO_EXECUTORS = ("host", "docker", "lxc")


def parse_forgejo_label(text):
    """(name, executor, image) from one Forgejo label, or raise ValueError
    saying what is wrong with it."""
    text = (text or "").strip()
    name, sep, rest = text.partition(":")
    if not name:
        raise ValueError(f"label {text!r} has no name")
    if not sep:
        return name, "host", ""
    executor, _, image = rest.partition("://")
    if executor not in FORGEJO_EXECUTORS:
        raise ValueError(f"label {text!r}: {executor!r} is not an executor "
                         f"forgejo-runner knows ({', '.join(FORGEJO_EXECUTORS)})")
    if executor == "host" and image:
        raise ValueError(f"label {text!r}: a host label takes no image")
    if executor != "host" and not image:
        raise ValueError(f"label {text!r}: {executor} needs an image, as "
                         f"{name}:{executor}://<image>")
    return name, executor, image


def forgejo_label(name, executor="host", image=""):
    """The label text for (name, executor, image) - the inverse of
    parse_forgejo_label, checked by round trip."""
    text = f"{name}:host" if executor == "host" else \
        f"{name}:{executor}://{image}"
    parse_forgejo_label(text)
    return text


def _forge_name(spec):
    """What the runner is called at the forge.

    Presentation only: the forge's identity for it is `registration_id`, and
    names are documented as non-unique there. The display name when there is
    one; otherwise a name built from the runner_id, so it is recognisable in
    the forge's list and never derived from anything a caller typed.
    """
    spec = spec or {}
    if spec.get("display_name"):
        return spec["display_name"]
    rid = str(spec.get("runner_id") or "")
    return f"rnr-{rid[:8]}" if rid else ""


@dataclass(frozen=True)
class Support:
    """Whether a cell exists, and why not when it does not.

    Truthy when supported, so `if provider.supports(p, a):` reads naturally,
    while the reason survives for the dashboard to render. A bare False would
    make an unavailable fleet indistinguishable from a broken one.
    """

    ok: bool
    reason: str = ""

    def __bool__(self):
        return self.ok


@dataclass(frozen=True)
class ArtifactRef:
    """Where the runner agent for one cell comes from.

    `source` is "vendor" when the forge publishes the binary and "self-built"
    when it does not. That distinction is not cosmetic: a self-built artefact
    has no upstream release feed to watch, which is the failure mode that has
    already bitten this fleet once when a pinned runner version was deprecated.
    """

    source: str
    reference: str
    notes: str = ""


@dataclass(frozen=True)
class RegistrationPlan:
    """What it takes to register one runner with its forge.

    `token` is short-lived and secret. Its field name is in REDACTED_FIELDS and
    it is never persisted: it goes into the create call and nowhere else.
    """

    url: str
    token: str
    name: str
    labels: str
    runner_group: str = ""
    extra: tuple = ()


@dataclass(frozen=True)
class DeregistrationPlan:
    """How a runner is removed from its forge.

    `via_api` matters. forgejo-runner has no `unregister` subcommand, so the
    only way to delete a Forgejo record is the API - which means a Forgejo
    container stopped any other way strands its registration, and a removal
    that cannot reach the forge must be refused rather than forced.
    """

    via_api: bool
    registration_id: str = ""
    registration_uuid: str = ""
    note: str = ""


@dataclass(frozen=True)
class DrainPlan:
    """How a runner stops taking jobs without losing the one it has (OPEN-7).

    `via_forge` is the whole question, and the two forges answer it opposite
    ways, because their runners treat a stop signal opposite ways.
    forgejo-runner finishes its job on SIGTERM and then exits, so it is drained
    on the worker. The GitHub runner cancels its job on SIGTERM, so it cannot
    be signalled at all while it works: it is drained at the forge instead,
    where GitHub stops giving it jobs, and it finishes the one it has undisturbed.
    """

    via_forge: bool
    note: str = ""


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

    # ---- the platform axis -------------------------------------------------

    def supports(self, platform, arch=X64, env=None):
        """Whether this forge can run on that platform, and why not if it
        cannot. Always answers; never raises on an unknown platform."""
        raise NotImplementedError

    def agent_artifact(self, platform, arch=X64, env=None):
        """Where the runner agent for that cell comes from, or None when the
        cell is unsupported."""
        raise NotImplementedError

    def deregistration(self, spec):
        """How to remove this runner from the forge."""
        raise NotImplementedError

    def registration(self, spec, env):
        """Everything it takes to register one runner, as (plan, error).

        Mints a short-lived registration token, so this talks to the forge.
        Returns an error rather than raising for the same reason
        container_env does: a registration that cannot happen must render as
        a reason, not as a 500. The error never contains the token.
        """
        raise NotImplementedError

    def default_labels(self, platform, arch=X64, env=None):
        """The labels a runner of that cell registers with when its fleet
        names none."""
        raise NotImplementedError

    def forge_records(self, env):
        """Every runner record the forge holds for this deployment, as a list,
        or None when the forge could not be asked."""
        raise NotImplementedError

    def delete_record(self, env, registration_id):
        """Delete one record at the forge. True when the forge confirmed it
        or it was already gone; False otherwise, never an exception."""
        raise NotImplementedError

    def record_for(self, spec, forge_records):
        """This runner's own record in the forge's list, or None."""
        raise NotImplementedError

    def registration_drift(self, expected, record):
        """How the forge's record differs from what the runner was registered
        with - labels and runner group - as one line, or None when they agree
        or the record says nothing either way (T-0902). Unknown is not drift:
        a record without labels proves nothing about them."""
        raise NotImplementedError

    def job_state(self, spec, forge_records):
        """busy, idle, offline or unknown, from the forge's own record.

        `forge_records` is what the forge client returned: a list, or None
        when the forge could not be asked. None is "unknown" and never "idle".
        That distinction is not pedantry - a wrong "idle" is what lets a cache
        clear delete layers a live build needs.
        """
        raise NotImplementedError

    def drain_plan(self, spec):
        """Where this runner is drained: at the forge or on its worker."""
        raise NotImplementedError

    def drain_at_forge(self, env, spec):
        """Stop the forge giving this runner jobs. Raises when the forge did
        not confirm it. Only for a provider whose plan is `via_forge`."""
        raise NotImplementedError(f"{self.key} runners drain on the worker")

    def undrain_at_forge(self, env, spec):
        """Let the forge give this runner jobs again. Idempotent: a runner
        that was never drained is left as its fleet asks."""
        raise NotImplementedError(f"{self.key} runners drain on the worker")


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

    #: What GitHub documents for self-hosted runners: Windows 10/11 and Server
    #: 2016/2019/2022 64-bit, macOS 11.0 or later, and the Linux list. x64 on
    #: all three; ARM64 on all three but in public preview.
    _SUPPORTED = {
        (LINUX, X64), (LINUX, ARM64),
        (WINDOWS, X64), (WINDOWS, ARM64),
        (MACOS, X64), (MACOS, ARM64),
    }

    def supports(self, platform, arch=X64, env=None):
        if (platform, arch) in self._SUPPORTED:
            return Support(True)
        if platform not in PLATFORMS:
            return Support(False, f"unknown platform {platform!r}")
        return Support(
            False,
            f"GitHub does not list {platform}/{arch} for self-hosted runners")

    def agent_artifact(self, platform, arch=X64, env=None):
        """GitHub publishes the runner for every cell it supports.

        The version is pinned in one place and must stay in step with the
        image: a runner whose version GitHub has deprecated stops being able
        to register at all, which takes out the whole fleet at once rather
        than one runner.
        """
        if not self.supports(platform, arch, env):
            return None
        version = (env or {}).get("RUNNER_VERSION") or os.environ.get(
            "RUNNER_VERSION", "2.336.0")
        return ArtifactRef(
            source="vendor",
            reference=f"actions/runner@v{version}",
            notes="published by GitHub; watch its release feed for "
                  "deprecations")

    #: The labels GitHub itself gives a self-hosted runner - `self-hosted`,
    #: the OS and the architecture, in GitHub's spelling - used when a fleet
    #: names none. The one platform table in this class, and the only thing
    #: about a platform registration needs to know (T-0901).
    _DEFAULT_LABELS = {
        (LINUX, X64): "self-hosted,Linux,X64",
        (LINUX, ARM64): "self-hosted,Linux,ARM64",
        (WINDOWS, X64): "self-hosted,Windows,X64",
        (WINDOWS, ARM64): "self-hosted,Windows,ARM64",
        (MACOS, X64): "self-hosted,macOS,X64",
        (MACOS, ARM64): "self-hosted,macOS,ARM64",
    }

    #: A deployment's own default per platform, when it sets one.
    #: RUNNER_LABELS is what the Linux fleet has always read; a Windows or
    #: macOS runner must not inherit it, or it registers as Linux and takes
    #: Linux jobs.
    _LABELS_ENV = {LINUX: "RUNNER_LABELS", WINDOWS: "RUNNER_LABELS_WINDOWS",
                   MACOS: "RUNNER_LABELS_MACOS"}

    #: GitHub's `os` field on a runner record, in this platform's words.
    _OS = {"linux": LINUX, "windows": WINDOWS, "macos": MACOS}

    def default_labels(self, platform, arch=X64, env=None):
        configured = (env or {}).get(self._LABELS_ENV.get(platform, ""), "")
        return configured.strip() or self._DEFAULT_LABELS.get(
            (platform, arch), "self-hosted")

    def platform_of(self, record):
        """Which platform GitHub says a registered runner is on, or None."""
        return self._OS.get(str((record or {}).get("os") or "").lower())

    def registration(self, spec, env):
        env = env or {}
        spec = spec or {}
        client = self.forge_client(env)
        if client is None:
            return None, ("GH_TOKEN and GITHUB_ORG are both required to "
                          "register a GitHub runner")
        token = client.registration_token()
        if not token:
            return None, ("GitHub issued no registration token - check that "
                          "GH_TOKEN may administer the org's runners and that "
                          "the API is reachable")
        return RegistrationPlan(
            url=f"https://github.com/{client.org}",
            token=token,
            name=_forge_name(spec),
            labels=_labels(spec, self.default_labels(
                spec.get("platform") or LINUX,
                spec.get("architecture") or X64, env)),
            runner_group=spec.get("runner_group")
            or env.get("RUNNER_GROUP", ""),
        ), None

    def forge_records(self, env):
        """The org's runners as GitHub reports them, or None when it could
        not be asked - never [] for a failure."""
        client = self.forge_client(env or {})
        return client.runners() if client is not None else None

    def delete_record(self, env, registration_id):
        """Delete one runner's record at GitHub by its id. True only when
        GitHub confirmed it, or said it was already gone."""
        client = self.forge_client(env or {})
        if client is None or not registration_id:
            return False
        return client.delete_runner(registration_id)

    def record_for(self, spec, forge_records):
        """Matched on the registration id GitHub gave it - never the name,
        which GitHub does not keep unique."""
        rid = str((spec or {}).get("registration_id") or "")
        if not rid or not forge_records:
            return None
        return next((r for r in forge_records
                     if isinstance(r, dict) and str(r.get("id")) == rid),
                    None)

    #: Labels GitHub adds to every self-hosted runner by itself. Their being
    #: there is not drift.
    _AUTOMATIC = frozenset({"self-hosted", "Linux", "Windows", "macOS", "X64",
                            "ARM64", "ARM"})

    def registration_drift(self, expected, record):
        if not isinstance(record, dict):
            return None
        expected = expected or {}
        parts = []
        if isinstance(record.get("labels"), list):
            want = {x.strip() for x in (expected.get("labels") or "").split(",")
                    if x.strip()}
            have = {str(x) for x in record["labels"]}
            missing = sorted(want - have)
            extra = sorted(have - want - self._AUTOMATIC)
            if missing:
                parts.append("missing labels " + ", ".join(missing))
            if extra:
                parts.append("unexpected labels " + ", ".join(extra))
        group, seen = expected.get("runner_group"), record.get("runner_group")
        if group and seen and seen != group:
            parts.append(f"runner group is {seen!r}, the fleet asks for "
                         f"{group!r}")
        return "; ".join(parts) or None

    def job_state(self, spec, forge_records):
        """From GitHub's runner list, matched on the registration id.

        GitHub reports `busy` directly, so nothing is inferred from a log -
        which is how the Linux fleet decides it today, and why a job whose
        completion line scrolled out of the tail used to read as still
        running.
        """
        record = self.record_for(spec, forge_records)
        if record is None:
            return UNKNOWN
        if record.get("busy"):
            return BUSY
        status = record.get("status")
        if status == "online":
            return IDLE
        if status == "offline":
            return OFFLINE
        return UNKNOWN

    def deregistration(self, spec):
        """GitHub runners deregister themselves.

        scripts/start.sh calls `config.sh remove` on SIGTERM, which is why the
        container carries a 60s stop timeout. The API path is not used, so a
        forge that is unreachable does not block a removal here.
        """
        return DeregistrationPlan(
            via_api=False,
            registration_id=str((spec or {}).get("registration_id") or ""),
            note="the runner deregisters itself on SIGTERM; give it the "
                 "full stop timeout")

    # ---- drain (OPEN-7) ------------------------------------------------------

    #: A runner group no repository may use. Set, and a drained runner is
    #: moved into it, which no job can follow it into. Unset, and its custom
    #: labels are taken off instead, which keeps every job that asks for one
    #: of them away - but not a job that asks only for `self-hosted`, the OS
    #: or the architecture, which GitHub will not let anyone take off. The
    #: reconciler covers that gap by looking again before it ends a drained
    #: runner; the group closes it.
    DRAIN_GROUP_ENV = "GITHUB_DRAIN_GROUP"

    def drain_plan(self, spec):
        return DrainPlan(
            via_forge=True,
            note="the GitHub runner cancels its job on SIGTERM, so it is "
                 "drained at GitHub, which stops giving it jobs, and "
                 "finishes the one it has undisturbed")

    def _runner_at_github(self, env, spec, doing):
        client = self.forge_client(env)
        if client is None:
            raise RuntimeError(f"GH_TOKEN and GITHUB_ORG are both required "
                               f"to {doing} a GitHub runner")
        rid = str((spec or {}).get("registration_id") or "")
        if not rid.isdigit():
            raise RuntimeError(f"no GitHub runner id on this runner, so "
                               f"nothing identifies it to {doing}")
        return client, rid

    def _custom_labels(self, spec, env, read_only):
        """The fleet's labels that GitHub counts as custom on this runner:
        all of them but the ones it gave the runner itself."""
        wanted = _labels(spec, self.default_labels(
            (spec or {}).get("platform") or LINUX,
            (spec or {}).get("architecture") or X64, env))
        return [x.strip() for x in wanted.split(",")
                if x.strip() and x.strip().lower() not in read_only]

    def drain_at_forge(self, env, spec):
        """Idempotent, because it is asked again on every pass until the
        runner is drained: a runner already in the drain group, or already
        without custom labels, is left so."""
        env = env or {}
        client, rid = self._runner_at_github(env, spec, "drain")
        group = (env.get(self.DRAIN_GROUP_ENV) or "").strip()
        if group:
            if not client.move_runner_to_group(group, rid):
                raise RuntimeError(f"GitHub did not confirm runner {rid} "
                                   f"moved into the drain group {group!r}")
            return
        labels = client.runner_labels(rid)
        if labels is None:
            raise RuntimeError(f"GitHub did not say which labels runner "
                               f"{rid} carries")
        read_only = {n.lower() for n, kind in labels if kind == "read-only"}
        if not self._custom_labels(spec, env, read_only):
            # Taking nothing off keeps no job away. Refused, not pretended.
            raise RuntimeError(
                f"runner {rid}'s fleet gives it no custom label to take off, "
                f"so GitHub would go on giving it jobs; set "
                f"{self.DRAIN_GROUP_ENV} to a runner group no repository "
                f"may use")
        left = client.remove_custom_labels(rid)
        if left is None or any(kind != "read-only" for _, kind in left):
            raise RuntimeError(f"GitHub did not confirm runner {rid}'s "
                               f"custom labels taken off")

    def undrain_at_forge(self, env, spec):
        """Back as its fleet asks: in the fleet's group, or carrying the
        fleet's labels. Also what a start does, so it must be - and is -
        harmless for a runner that was never drained."""
        env = env or {}
        client, rid = self._runner_at_github(env, spec, "put back in service")
        if (env.get(self.DRAIN_GROUP_ENV) or "").strip():
            home = ((spec or {}).get("runner_group")
                    or env.get("RUNNER_GROUP") or "Default")
            if not client.move_runner_to_group(home, rid):
                raise RuntimeError(f"GitHub did not confirm runner {rid} "
                                   f"moved back into {home!r}")
            return
        labels = client.runner_labels(rid)
        if labels is None:
            raise RuntimeError(f"GitHub did not say which labels runner "
                               f"{rid} carries")
        read_only = {n.lower() for n, kind in labels if kind == "read-only"}
        custom = self._custom_labels(spec, env, read_only)
        have = {n.lower() for n, kind in labels if kind != "read-only"}
        if not custom or have == {x.lower() for x in custom}:
            return
        if client.set_custom_labels(rid, custom) is None:
            raise RuntimeError(f"GitHub did not confirm runner {rid}'s "
                               f"labels put back")


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
            return {}, "FORGEJO_API_TOKEN is not set"
        token = client.registration_token()
        if not token:
            return {}, ("could not mint a registration token - check "
                        "FORGEJO_API_TOKEN and that Forgejo is reachable")
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
        token = (env.get("FORGEJO_API_TOKEN") or "").strip()
        if not (url and token):
            return None
        return forgejo_api.Forgejo(url, token)

    #: Forgejo publishes linux/amd64 and linux/arm64 and nothing else. Release
    #: v13.1.0 carries twelve assets, all Linux. The project's Makefile has a
    #: DARWIN_ARCHS variable that no release target consumes - dead
    #: configuration inherited upstream, not evidence of a build.
    _VENDOR_SUPPORTED = {(LINUX, X64), (LINUX, ARM64)}

    #: The env key that names a self-built agent for a platform Forgejo does
    #: not publish. Set it and the cell becomes available; leave it unset and
    #: the cell reports itself unavailable with the reason, rather than
    #: failing later at registration.
    _ARTIFACT_KEY = {
        WINDOWS: "FORGEJO_RUNNER_ARTIFACT_WINDOWS",
        MACOS: "FORGEJO_RUNNER_ARTIFACT_MACOS",
    }

    def supports(self, platform, arch=X64, env=None):
        if (platform, arch) in self._VENDOR_SUPPORTED:
            return Support(True)
        if platform not in PLATFORMS:
            return Support(False, f"unknown platform {platform!r}")
        key = self._ARTIFACT_KEY.get(platform)
        if key and (env or {}).get(key, "").strip():
            return Support(True)
        if key:
            return Support(
                False,
                f"Forgejo publishes no {platform} runner binary; set {key} to "
                f"a self-built artefact to enable this fleet")
        return Support(False, f"Forgejo does not publish {platform}/{arch}")

    def agent_artifact(self, platform, arch=X64, env=None):
        if not self.supports(platform, arch, env):
            return None
        if (platform, arch) in self._VENDOR_SUPPORTED:
            return ArtifactRef(
                source="vendor",
                reference=f"forgejo/runner:{platform}-"
                          f"{'amd64' if arch == X64 else 'arm64'}",
                notes="published at code.forgejo.org/forgejo/runner/releases")
        key = self._ARTIFACT_KEY[platform]
        return ArtifactRef(
            source="self-built",
            reference=(env or {}).get(key, "").strip(),
            notes="built from source; there is no upstream release feed for "
                  "this platform, so its version has to be tracked by hand")

    #: A deployment's label default per platform. FORGEJO_RUNNER_LABELS is
    #: what the Linux fleet has always read; a Windows or macOS runner must
    #: not inherit it - its `docker://` labels would send it jobs it has no
    #: engine to run.
    _LABELS_ENV = {LINUX: "FORGEJO_RUNNER_LABELS",
                   WINDOWS: "FORGEJO_RUNNER_LABELS_WINDOWS",
                   MACOS: "FORGEJO_RUNNER_LABELS_MACOS"}

    #: Which executors a runner on each platform can offer. Only Linux has an
    #: engine; a Windows or macOS runner runs jobs on its own OS (T-1003).
    _EXECUTORS = {LINUX: frozenset(FORGEJO_EXECUTORS),
                  WINDOWS: frozenset({"host"}),
                  MACOS: frozenset({"host"})}

    #: When neither the fleet nor the deployment names labels. Linux has no
    #: default - which image jobs run in is a choice nobody should make by
    #: omission - so a Linux runner with no labels is refused, as it always
    #: was. Windows and macOS can only run on the host, so that choice is
    #: already made.
    _DEFAULT_LABELS = {WINDOWS: "windows:host", MACOS: "macos:host"}

    def default_labels(self, platform, arch=X64, env=None):
        configured = ((env or {}).get(self._LABELS_ENV.get(platform, ""))
                      or "").strip()
        return configured or self._DEFAULT_LABELS.get(platform, "")

    def label_problem(self, platform, labels):
        """Why these labels cannot be registered on this platform, or None.
        Checked before a token is minted."""
        allowed = self._EXECUTORS.get(platform, frozenset())
        for text in (x for x in labels.split(",") if x.strip()):
            try:
                name, executor, _ = parse_forgejo_label(text)
            except ValueError as e:
                return str(e)
            if executor not in allowed:
                return (f"label {text.strip()!r} asks for the {executor} "
                        f"executor, which a {platform} runner does not have; "
                        f"use {name}:host")
        return None

    def forge_records(self, env):
        """This user's runner records, or None when Forgejo could not be
        asked - never [] for a failure."""
        client = self.forge_client(env or {})
        return client.runner_statuses() if client is not None else None

    def delete_record(self, env, registration_id):
        """The only way a Forgejo record is ever deleted: forgejo-runner has
        no `unregister` (T-1002)."""
        client = self.forge_client(env or {})
        if client is None or not registration_id:
            return False
        return client.delete_runner(registration_id)

    def registration(self, spec, env):
        """Checked in the same order as container_env, and for the same
        reason: every refusal that needs no network comes before the token is
        minted, so a registration that cannot succeed does not burn one."""
        env = env or {}
        spec = spec or {}
        platform = spec.get("platform") or LINUX
        arch = spec.get("architecture") or X64
        support = self.supports(platform, arch, env)
        if not support:
            # An unavailable cell is an answer, not a crash (T-1001).
            return None, support.reason
        url = (env.get("FORGEJO_INSTANCE_URL") or "").strip()
        if not url:
            return None, "FORGEJO_INSTANCE_URL is not set"
        labels = _labels(spec, self.default_labels(platform, arch, env))
        if not labels:
            return None, ("no labels for this runner - a runner with no "
                          "labels registers, looks healthy, and never picks "
                          "up a job")
        problem = self.label_problem(platform, labels)
        if problem:
            return None, problem
        client = self.forge_client(env)
        if client is None:
            return None, "FORGEJO_API_TOKEN is not set"
        token = client.registration_token()
        if not token:
            return None, ("could not mint a registration token - check "
                          "FORGEJO_API_TOKEN and that Forgejo is reachable")
        return RegistrationPlan(url=url, token=token, name=_forge_name(spec),
                                labels=labels), None

    #: Forgejo's words for a runner's state, and what each means here. Any
    #: word not in this table reads as unknown - from a future release or a
    #: bug, assuming it harmless is what would let a cache clear act on a
    #: runner that is working.
    _STATUS = {"active": BUSY, "idle": IDLE, "offline": OFFLINE}

    def job_state(self, spec, forge_records):
        """From Forgejo's runner records, matched on the registration uuid.

        The same reading docker_ops._forgejo_job_state does today, except that
        idle and offline stay distinct: the platform reports a runner ready
        only when the forge shows it online, and a process that is up while
        the forge says offline is the failure that has already been missed
        once on this fleet.
        """
        record = self.record_for(spec, forge_records)
        if record is None:
            return UNKNOWN
        return self._STATUS.get(record.get("status") or "", UNKNOWN)

    def record_for(self, spec, forge_records):
        """Matched on the registration uuid, which Forgejo's records carry
        and its ids alone do not identify across instances."""
        uuid = (spec or {}).get("registration_uuid")
        if not uuid or not forge_records:
            return None
        return next((r for r in forge_records
                     if isinstance(r, dict) and r.get("uuid") == uuid), None)

    def registration_drift(self, expected, record):
        """Forgejo keeps a label's name; the `:docker://image` part tells the
        runner how to run it and is not what a job matches on. So names are
        compared, both sides reduced to the part before the first colon."""
        if not isinstance(record, dict) or not isinstance(
                record.get("labels"), list):
            return None
        want = {x.split(":", 1)[0].strip()
                for x in ((expected or {}).get("labels") or "").split(",")
                if x.strip()}
        have = {str(x).split(":", 1)[0] for x in record["labels"]}
        parts = []
        if want - have:
            parts.append("missing labels " + ", ".join(sorted(want - have)))
        if have - want:
            parts.append("unexpected labels " + ", ".join(sorted(have - want)))
        return "; ".join(parts) or None

    def deregistration(self, spec):
        """Only the API can delete a Forgejo runner record.

        forgejo-runner has no `unregister` subcommand, so a container stopped
        any other way - `docker stop`, `compose down`, the host going down -
        leaves an offline runner behind in Forgejo for ever. A removal that
        cannot reach the API must therefore be refused rather than forced,
        which is the opposite of the GitHub case.
        """
        spec = spec or {}
        return DeregistrationPlan(
            via_api=True,
            registration_id=str(spec.get("registration_id") or ""),
            registration_uuid=str(spec.get("registration_uuid") or ""),
            note="forgejo-runner cannot deregister itself; refuse the removal "
                 "if the API is unreachable rather than stranding the record")

    def drain_plan(self, spec):
        """forgejo-runner finishes the job it has on SIGTERM and takes no
        other - its daemon waits up to `shutdown_timeout`, three hours by
        default - so it is drained on its worker, and its record at Forgejo
        is not touched."""
        return DrainPlan(
            via_forge=False,
            note="forgejo-runner finishes its job on SIGTERM, then exits; the "
                 "unit is kept down")


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
