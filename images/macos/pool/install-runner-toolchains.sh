#!/usr/bin/env bash
set -euo pipefail

# Run as the same user that owns both macOS runner listeners.
export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
if [ "$(uname -m)" != x86_64 ]; then
  echo 'This macOS runner base is expected to be x86_64.' >&2
  exit 1
fi

if ! command -v rustup >/dev/null 2>&1; then
  brew install rustup
  brew link --force rustup
fi
rustup default stable
# Homebrew's rustup formula supplies the manager but no proxy commands.
# Link the selected toolchain binaries; the stable toolchain path remains
# stable across rustup updates.
for command in rustc cargo rustfmt clippy-driver; do
  binary="$(rustup which "$command" 2>/dev/null || true)"
  if [ -n "$binary" ]; then
    ln -sf "$binary" "/usr/local/bin/$command"
  fi
done
rustc --version | grep -q '^rustc '
cargo --version | grep -q '^cargo '

sdk="$HOME/Library/Android/sdk"
tools="$sdk/cmdline-tools/latest/bin/sdkmanager"
if [ ! -x "$tools" ]; then
  archive="$HOME/Downloads/commandlinetools-mac_x86_64-15859902_latest.zip"
  unpacked="$HOME/Downloads/nomercy-android-tools-15859902"
  mkdir -p "$HOME/Downloads" "$unpacked" "$sdk/cmdline-tools/latest"
  curl -fsSL 'https://dl.google.com/android/repository/commandlinetools-mac_x86_64-15859902_latest.zip' -o "$archive"
  printf '%s  %s\n' 'c5a6378ab5cf7e0d5701921405115befff13e9ff7417fb588389338f8bd050f3' "$archive" | shasum -a 256 -c -
  unzip -q -o "$archive" -d "$unpacked"
  cp -R "$unpacked/cmdline-tools/." "$sdk/cmdline-tools/latest/"
fi
export ANDROID_HOME="$sdk" ANDROID_SDK_ROOT="$sdk"
set +o pipefail
yes y | "$tools" --sdk_root="$sdk" --licenses >/dev/null
set -o pipefail
"$tools" --sdk_root="$sdk" 'platform-tools' 'platforms;android-35' 'build-tools;35.0.0'
ln -sf "$tools" /usr/local/bin/sdkmanager
ln -sf "$sdk/platform-tools/adb" /usr/local/bin/adb

test -f "$sdk/platforms/android-35/android.jar"
test -x "$sdk/platform-tools/adb"
test -x "$sdk/build-tools/35.0.0/aapt2"
xcrun --find clang >/dev/null
echo 'macOS runner toolchains ready: Xcode, Rust and Android SDK.'
