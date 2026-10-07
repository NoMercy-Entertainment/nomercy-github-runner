#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
trap 'echo; read -r -p "Druk op Enter om dit venster te sluiten." _answer' EXIT
if [[ "$(uname -m)" != arm64 ]]; then
  echo 'Dit pakket vereist een Apple Silicon Mac. Open Terminal zonder Rosetta.'
  exit 1
fi
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if ! command -v brew >/dev/null; then
  echo 'Homebrew wordt eenmalig geinstalleerd. macOS kan om toestemming/wachtwoord vragen.'
  /bin/bash bootstrap/homebrew-install.sh
fi
missing=()
command -v qemu-system-aarch64 >/dev/null || missing+=(qemu)
command -v swtpm >/dev/null || missing+=(swtpm)
[[ -x /opt/homebrew/bin/python3 ]] || missing+=(python)
if [[ ${#missing[@]} -gt 0 ]]; then
  brew install "${missing[@]}"
fi
/opt/homebrew/bin/python3 portable.py start
