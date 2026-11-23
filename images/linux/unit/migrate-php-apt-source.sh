#!/bin/sh
set -eu

. /etc/os-release
test "$ID" = ubuntu && test "$VERSION_CODENAME" = noble || exit 1

sources=/etc/apt/sources.list.d
old_source="$sources/ondrej-ubuntu-php-noble.sources"
new_source="$sources/nomercy-sury-php.sources"
backup=/var/backups/nomercy
keyring=/etc/apt/keyrings/nomercy-sury-php.gpg
if [ -f "$old_source" ]; then
    grep -Eq '^URIs: https://ppa\.launchpadcontent\.net/ondrej/php/ubuntu/?$' "$old_source"
    grep -Fxq 'Suites: noble' "$old_source"
fi

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
curl -fsSL --retry 3 --max-time 20 \
    https://packages.sury.org/debsuryorg-archive-keyring.deb -o "$tmp/keyring.deb"
printf '%s  %s\n' 7511384559c9ddf1d5ce5f60be429ae9d4e7d01d9480d6f1b7a30c0810cf8b60 \
    "$tmp/keyring.deb" | sha256sum -c -
# Extract the pinned vendor key without running package scripts or changing dpkg state.
dpkg-deb -x "$tmp/keyring.deb" "$tmp/key"
install -d -m 0755 /etc/apt/keyrings
install -m 0644 "$tmp/key/usr/share/keyrings/debsuryorg-archive-keyring.gpg" "$keyring"
cat > "$tmp/php.sources" <<EOF
Types: deb
URIs: https://packages.sury.org/php/
Suites: noble
Components: main
Signed-By: $keyring
EOF
install -m 0644 "$tmp/php.sources" "$new_source"
if [ -f "$old_source" ]; then
    install -d -m 0700 "$backup"
    mv "$old_source" "$backup/"
fi
