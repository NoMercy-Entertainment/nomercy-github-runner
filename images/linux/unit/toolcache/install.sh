#!/usr/bin/env bash
# Put the hosted tool cache into an image: every row of versions.txt for this
# Ubuntu (Node rows are for any), installed by the build's own setup.sh into
# DEST in the layout setup-python and setup-node look in:
#
#   DEST/Python/3.12.16/x64/...   DEST/Python/3.12.16/x64.complete
#   DEST/node/22.23.4/x64/...     DEST/node/22.23.4/x64.complete
#
# Each download is checked against its pinned sha256 before anything in it
# runs, and each installed version must start and say its own version.
#
#   install.sh 24.04 /opt/hostedtoolcache
set -euo pipefail
os_version="${1:?usage: install.sh UBUNTU_VERSION DEST}"
dest="${2:?usage: install.sh UBUNTU_VERSION DEST}"
here="$(cd "$(dirname "$0")" && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$dest"

installed=0
while read -r kind version ubuntu url digest; do
  case "$kind" in ''|'#'*) continue ;; esac
  if [ "$kind" = python ] && [ "$ubuntu" != "$os_version" ]; then
    continue
  fi
  echo "tool cache: $kind $version ($ubuntu)"
  archive="$work/$kind-$version.tar.gz"
  curl -fsSL --retry 5 --retry-delay 5 -o "$archive" "$url"
  echo "${digest#sha256:}  $archive" | sha256sum -c -
  unpack="$work/$kind-$version"
  mkdir -p "$unpack"
  tar -xzf "$archive" -C "$unpack"
  rm -f "$archive"
  # setup.sh reads AGENT_TOOLSDIRECTORY (node) or, without it,
  # RUNNER_TOOL_CACHE (python): both are DEST here.
  (cd "$unpack" && AGENT_TOOLSDIRECTORY="$dest" RUNNER_TOOL_CACHE="$dest" \
     PIP_ROOT_USER_ACTION=ignore bash ./setup.sh >/dev/null)
  rm -rf "$unpack"
  case "$kind" in
    python)
      dir="$dest/Python/$version"
      test -f "$dir/x64.complete"
      "$dir/x64/bin/python3" --version | grep -qx "Python $version"
      "$dir/x64/bin/python3" -c 'import ssl, sqlite3, ctypes, bz2, lzma, zlib, hashlib, venv'
      ;;
    node)
      dir="$dest/node/$version"
      test -f "$dir/x64.complete"
      "$dir/x64/bin/node" --version | grep -qx "v$version"
      ;;
    *) echo "unknown kind $kind" >&2; exit 65 ;;
  esac
  installed=$((installed + 1))
done < "$here/versions.txt"

[ "$installed" -gt 0 ] || { echo "nothing installed for Ubuntu $os_version" >&2; exit 66; }
# Readable by whichever user a job runs as; the setup scripts keep root's umask.
chmod -R a+rX "$dest"
echo "tool cache: $installed versions in $dest"
