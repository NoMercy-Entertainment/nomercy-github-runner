#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
trap 'echo; read -r -p "Druk op Enter om dit venster te sluiten." _answer' EXIT
/opt/homebrew/bin/python3 portable.py return
