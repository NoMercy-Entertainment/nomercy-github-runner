"""Compiling overlays must retain findings and their correct tracked location."""
import importlib.util
from pathlib import Path


def test_codeql_mapping_preserves_findings_flows_and_upstream_locations():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("codeql_mapping", root / "infra/ci/map-codeql-forgejo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tracked = {"uri": ".codeql-forgejo-source/internal/app/run/admission.go", "uriBaseId": "%SRCROOT%"}
    flow = {"uri": "file:///workspace/.codeql-forgejo-source/internal/pkg/report/event.go"}
    upstream = {"uri": ".codeql-forgejo-source/internal/app/run/runner.go"}
    result = {"ruleId": "security-rule", "message": {"text": "finding"},
              "locations": [{"physicalLocation": {"artifactLocation": tracked, "region": {"startLine": 42}}}],
              "codeFlows": [{"threadFlows": [{"locations": [{"location": {"physicalLocation": {"artifactLocation": flow}}}]}]}]}
    document = {"runs": [{"results": [result], "artifacts": [{"location": upstream}]}]}
    module.remap(document)
    assert document["runs"][0]["results"] == [result]
    assert result["ruleId"] == "security-rule" and result["message"]["text"] == "finding"
    assert result["locations"][0]["physicalLocation"]["region"] == {"startLine": 42}
    assert tracked["uri"] == "images/forgejo-admission/admission.go"
    assert flow == {"uri": "images/forgejo-events/event.go", "uriBaseId": "%SRCROOT%"}
    assert upstream["uri"] == ".codeql-forgejo-source/internal/app/run/runner.go"
