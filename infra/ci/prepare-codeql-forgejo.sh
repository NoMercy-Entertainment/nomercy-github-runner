#!/usr/bin/env bash
# Assemble exactly the same reviewed source and patches as the guarded build.
set -euo pipefail
repository="$(git rev-parse --show-toplevel)"
source="$repository/.codeql-forgejo-source"
commit=6095cb17bfdbded5aa4ea84c18a4b69fd9574cca
test ! -e "$source"
git clone --quiet --depth 1 --branch v13.1.0 https://code.forgejo.org/forgejo/runner.git "$source"
test "$(git -C "$source" rev-parse HEAD)" = "$commit"
python3 "$repository/images/forgejo-events/apply.py" "$source"
python3 "$repository/images/forgejo-admission/apply.py" "$source"
python3 "$repository/images/forgejo-security/apply.py" "$source"
unset GOROOT
test "$(go env GOVERSION)" = go1.26.7
test "$(go tool compile -V)" = 'compile version go1.26.7'
# Fail preparation rather than upload an incomplete analysis.
(cd "$source" && GOTOOLCHAIN=local go mod download)
