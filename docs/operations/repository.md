# Repository ownership

The active repository is **FiLL/nomercy-runners**:

```sh
git clone https://forgejo.phillippepelzer.me/FiLL/nomercy-runners.git
```

This is a private repository. Use an authorized Forgejo account and a Git
credential helper; do not put a token in a clone URL or a tracked file.
The default branch remains `master`, with the original commits and authors intact.

## Working copy

The Windows working copy remains `D:\docker-compose\GithubRunners`.
Its `origin` and default push destination point to Forgejo. The former GitHub
remote is retained as `legacy-github` for reading history, with a disabled push
URL. No changes from this migration are pushed to GitHub.

The imported `.github/workflows` retain their original GitHub labels and
settings. Repository Actions are disabled during the migration; adapt those
workflows for Forgejo before enabling automatic runs in this repository.
This setting does not change runner registrations or other repositories' jobs.

## Before the next dashboard redesign

The current UI and all local source changes were backed up on 2026-09-26 to:

```text
D:\HyperV\runner-platform\backups\before-forgejo-20260926-142945
```

The verified backup includes a Git bundle, the Git directory, and a working tree
archive including uncommitted and untracked source files. It also contains local
environment files, so it stays on the host under restricted access and must not
be uploaded. Generated runner downloads and Python caches are excluded.

Restore into a new empty directory using `RESTORE.txt` in that backup.
The dashboard image before the new redesign is
`nomercy/runner-dashboard:ui-sleak-20260926f`. The repository tag
`backup/ui-before-redesign-20260926` marks the same UI source.
