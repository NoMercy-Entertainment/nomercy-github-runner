#!/usr/bin/env python3
"""Install immutable, hash-verified templates inside the macOS guest."""
import argparse
import hashlib
from pathlib import Path
import shutil
import subprocess
import tempfile

ARTIFACTS = {
    "github": ("actions-runner-osx-x64-2.338.0.tar.gz",
               "dea7a58796ce215fc424a8b27c0fdd9b813fa5fc03d109a4a1d38131d6bfa1a3",
               "actions-runner-v2.338.0-macos-r20261008"),
    "forgejo": ("forgejo-runner-v13.1.0-darwin-amd64",
                "f9f9ed421d6d1e71436b92e9b8891fb03b44c713e87e545c6e86423be2edb07a",
                "forgejo-runner-v13.1.0-macos-r20260921"),
}


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def install(artifacts, templates):
    source = Path(__file__).parent / "template"
    templates.mkdir(parents=True, exist_ok=True)
    for provider, (filename, expected, name) in ARTIFACTS.items():
        artifact = artifacts / filename
        if digest(artifact) != expected:
            raise RuntimeError("artifact SHA-256 mismatch: " + filename)
        target = templates / name
        if target.exists():
            if (target / ".artifact-sha256").read_text().strip() != expected:
                raise RuntimeError("existing template has different provenance: " + name)
            print("kept", name)
            continue
        stage = Path(tempfile.mkdtemp(prefix=".install-", dir=templates))
        try:
            for entry in ("run", "register", "deregister"):
                shutil.copyfile(source / entry, stage / entry)
                (stage / entry).chmod(0o755)
            (stage / "provider").write_text(provider + "\n")
            if provider == "github":
                (stage / "agent").mkdir()
                subprocess.run(["/usr/bin/tar", "-xzf", str(artifact.resolve()),
                                "-C", str(stage / "agent")], check=True)
            else:
                shutil.copyfile(artifact, stage / "forgejo-runner")
                (stage / "forgejo-runner").chmod(0o755)
            (stage / ".artifact-sha256").write_text(expected + "\n")
            stage.chmod(0o755)
            stage.rename(target)
            print("installed", name)
        finally:
            if stage.exists():
                shutil.rmtree(stage)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--templates", type=Path, default=Path("/Users/runner/templates"))
    args = parser.parse_args()
    install(args.artifacts, args.templates)
