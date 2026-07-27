

# GitHub Actions Self-Hosted Runner (Docker)

This project provides a Dockerized GitHub Actions runner that supports scaling, custom labels, runner groups, and Docker-in-Docker (DinD) for CI/CD workflows.

## Quick Start

### 1. Clone and configure

Clone this repo and copy `.env.example` to `.env`:

```sh
cp .env.example .env
```

Edit `.env` and set:

- `GH_TOKEN` — GitHub PAT with admin:org or repo scope
- `GITHUB_ORG` — Your GitHub organization name
- `RUNNER_LABELS` — (Optional) Comma-separated labels for your runner (e.g. `yourname,team,customtag`)
- `RUNNER_GROUP` — (Optional) Runner group name (must exist in your org)

### 2. Build and run with Docker Compose

```sh
docker compose up --build -d
```

#### To scale runners:

```sh
docker compose -f docker-compose.runners.yml up -d
```

Runners are defined as six explicit services rather than `deploy.replicas`.
Replicas all receive identical mounts, and several Docker-in-Docker daemons
sharing one data root corrupt each other.

## Setting up runners on another machine

To add runners from a different machine, use the guided installers in
[`install/`](install/README.md) rather than this compose file. They work on
Windows, Linux and macOS, and give the runners their own storage separate from
whatever Docker that machine already runs.

### 3. Docker-in-Docker (DinD)

Each runner starts its **own** Docker daemon inside the container. The host's
Docker socket is **not** mounted, so a job cannot see or touch containers
outside its own runner.

The inner daemon uses the `fuse-overlayfs` storage driver: the kernel cannot
stack native `overlay2` on top of the host's overlay filesystem when the
container is itself an overlay mount.

Build cache in that inner daemon is capped by a `builder.gc` policy in
`scripts/start.sh`, and a janitor loop sweeps dead images, stale workspaces and
old diagnostic logs every six hours. Without those the daemons grow without
limit - this is not theoretical, they once filled a 1 TB disk.

## Environment Variables

Set these in your `.env` file:

| Variable        | Description                                      |
|-----------------|--------------------------------------------------|
| **GH_TOKEN**        | GitHub PAT (admin:org or repo scope)             |
| **GITHUB_ORG**      | GitHub organization name                         |
| **RUNNER_LABELS**   | (Optional) Comma-separated runner labels         |
| **RUNNER_GROUP**    | (Optional) Runner group name                     |


## Compose Configuration

See the included `docker-compose.yml` file in this repository for the latest and recommended configuration example.

## Token Verification

You can verify your token with:

```sh
curl -L -X POST \
	-H "Accept: application/vnd.github+json" \
	-H "Authorization: Bearer {token}" \
	https://api.github.com/orgs/{org}/actions/runners/registration-token
```

## Notes
- The runner name is randomized per instance.
- The container runs as root and `privileged`, which its own Docker daemon requires.
- For repository-level runners, adjust the API endpoint and variables accordingly.