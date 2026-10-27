#!/bin/bash
# Fill a hosted tool cache with the Python and Node versions jobs ask for, the
# way actions/setup-python and actions/setup-node would install them, so a job
# finds them in the cache and downloads nothing.
#
# Run as the listener account (`user`) inside a guest:
#   bash install-runner-toolcache.sh [TOOLCACHE]
# TOOLCACHE defaults to /Users/runner/hostedtoolcache, GitHub's hosted macOS
# path and the RUNNER_TOOL_CACHE / AGENT_TOOLSDIRECTORY of every pool runner
# (agent/runtimes/macos_pool.py, HOSTED_TOOLCACHE). It must be that path: the
# python-versions packages python.org has no installer for (3.10.22 here,
# every 3.9 and 3.8) are compiled with it as their prefix and load libpython
# from it by absolute path; anywhere else they abort in dyld.
#
# Python: the official actions/python-versions darwin-x64 package and its own
# setup.sh, run from the unpacked package with AGENT_TOOLSDIRECTORY set, exactly
# as setup-python does. For 3.11 and later that setup.sh installs the python.org
# framework under /Library/Frameworks/Python.framework/Versions/<major.minor>
# (sudo installer) and links it into <TOOLCACHE>/Python/<version>/x64; for
# 3.10.22 it copies the prebuilt tree there. Then x64.complete.
#
# Node: the official darwin-x64 tarball, checked against SHASUMS256.txt,
# unpacked into <TOOLCACHE>/node/<version>/x64 with x64.complete beside it,
# the layout tool-cache's cacheDir gives setup-node.
set -euo pipefail

TOOLCACHE=${1:-/Users/runner/hostedtoolcache}
PYTHON_VERSIONS=(3.10.22 3.11.9 3.12.10 3.13.17 3.14.10)
NODE_VERSIONS=(20.20.2 22.23.4 24.22.1 26.12.1)
MANIFEST=https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json

work=$(mktemp -d /tmp/toolcache.XXXXXX)
trap 'rm -rf "$work"' EXIT
mkdir -p "$TOOLCACHE"

# Every package URL is resolved before the first install: the python.org
# installers relink /usr/local/bin/python3 as they go. /usr/bin/python3 is
# the Command Line Tools stub and can block on a GUI prompt, so it is not used.
curl -fsSL --retry 3 "$MANIFEST" -o "$work/manifest.json"
/usr/local/bin/python3 - "$work/manifest.json" "${PYTHON_VERSIONS[@]}" > "$work/urls" <<'PY'
import json, sys
releases = {r["version"]: r for r in json.load(open(sys.argv[1]))}
for version in sys.argv[2:]:
    files = [f for f in releases[version]["files"]
             if f["platform"] == "darwin" and f["arch"] == "x64"]
    if not files:
        sys.exit("no darwin-x64 package for " + version)
    print(version, files[0]["download_url"])
PY
for version in "${PYTHON_VERSIONS[@]}"; do
  if [ -d "$TOOLCACHE/Python/$version/x64" ] && [ -f "$TOOLCACHE/Python/$version/x64.complete" ]; then
    echo "Python $version: already in $TOOLCACHE"; continue
  fi
  url=$(awk -v v="$version" '$1 == v {print $2}' "$work/urls")
  echo "Python $version: $url"
  rm -rf "$work/py"; mkdir -p "$work/py"
  curl -fsSL --retry 3 "$url" -o "$work/py.tgz"
  tar -xzf "$work/py.tgz" -C "$work/py"
  (cd "$work/py" && AGENT_TOOLSDIRECTORY="$TOOLCACHE" RUNNER_TOOL_CACHE="$TOOLCACHE" bash ./setup.sh)
  test -f "$TOOLCACHE/Python/$version/x64.complete"
done

curl -fsSL --retry 3 https://nodejs.org/dist/index.json -o "$work/node-index.json"
for version in "${NODE_VERSIONS[@]}"; do
  target="$TOOLCACHE/node/$version/x64"
  if [ -d "$target" ] && [ -f "$target.complete" ]; then
    echo "Node $version: already in $TOOLCACHE"; continue
  fi
  name="node-v$version-darwin-x64"
  echo "Node $version"
  curl -fsSL --retry 3 "https://nodejs.org/dist/v$version/$name.tar.gz" -o "$work/$name.tar.gz"
  curl -fsSL --retry 3 "https://nodejs.org/dist/v$version/SHASUMS256.txt" -o "$work/SHASUMS256.txt"
  (cd "$work" && grep " $name.tar.gz\$" SHASUMS256.txt | shasum -a 256 -c -)
  rm -rf "$target" "$target.complete"
  mkdir -p "$target"
  tar -xzf "$work/$name.tar.gz" -C "$target" --strip-components 1
  touch "$target.complete"
done

# The python.org installers link python3 and pip3 into /usr/local/bin, but
# no bare `pip` or `python`; jobs written for GitHub's images call both.
for pair in pip:pip3 python:python3; do
  name=${pair%%:*}; target=${pair#*:}
  if [ ! -e "/usr/local/bin/$name" ] && [ -e "/usr/local/bin/$target" ]; then
    sudo ln -s "$target" "/usr/local/bin/$name"
  fi
done
/usr/local/bin/pip --version
/usr/local/bin/python --version

echo "Tool cache $TOOLCACHE:"
for d in "$TOOLCACHE"/*/*; do
  [ -f "$d/x64.complete" ] && echo "  ${d#"$TOOLCACHE"/} x64 complete"
done
"$TOOLCACHE/Python/3.12.10/x64/python" --version
"$TOOLCACHE/node/22.23.4/x64/bin/node" --version
