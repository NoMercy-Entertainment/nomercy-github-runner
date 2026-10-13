"""Preserve every SARIF result; map exact compiled copies to tracked overlays."""
import json
from pathlib import Path
import sys
from urllib.parse import unquote

COPIES = {
    ".codeql-forgejo-source/internal/pkg/report/event.go": "images/forgejo-events/event.go",
    ".codeql-forgejo-source/internal/pkg/report/event_test.go": "images/forgejo-events/event_test.go",
    ".codeql-forgejo-source/internal/app/run/admission.go": "images/forgejo-admission/admission.go",
    ".codeql-forgejo-source/internal/app/run/admission_test.go": "images/forgejo-admission/admission_test.go",
}

def remap(document):
    if isinstance(document, dict):
        # artifactLocation objects also occur in code-flow steps and related
        # locations, not only the primary result. Line numbers stay identical.
        uri = document.get("uri")
        if isinstance(uri, str):
            decoded = unquote(uri).replace("\\\\", "/")
            for copied, tracked in COPIES.items():
                if decoded == copied or decoded.endswith("/" + copied):
                    document["uri"] = tracked
                    document["uriBaseId"] = "%SRCROOT%"
                    break
        for value in document.values():
            remap(value)
    elif isinstance(document, list):
        for value in document:
            remap(value)

def main(directory):
    files = list(Path(directory).glob("*.sarif"))
    if not files:
        raise SystemExit("CodeQL produced no SARIF files")
    root = Path(__file__).resolve().parents[2]
    for copied, tracked in COPIES.items():
        if (root / copied).read_bytes() != (root / tracked).read_bytes():
            raise SystemExit("Compiled overlay differs from its tracked source")
    for file in files:
        document = json.loads(file.read_text())
        remap(document)
        file.write_text(json.dumps(document), encoding="utf-8")

if __name__ == "__main__":
    main(sys.argv[1])
