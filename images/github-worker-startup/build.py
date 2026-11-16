"""Rebuild the pinned Worker with the Listener's supported IPC timeout policy.

Only Runner.Worker.dll is replaced. All dependencies come from the verified
upstream runner distribution in the base image; restore has no network source.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import xml.etree.ElementTree as ET

VERSION = "2.338.0"
COMMIT = "197ca027b90de199464dee3bb9ff198a1db60967"
SOURCE_SHA256 = "f9ad169b07995dad7964a4f9ce95673a153317a4888ba8d540ecead71def725a"
ORIGINAL_SHA256 = "5b70067b6cbf706bcccdc73ef4a3ce0b118a86ad5cdf82b54dcca96724c67a0f"
PATCH_VERSION = "1"
SDK = "8.0.427"


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def patch_worker(path):
    text = path.read_text(encoding="utf-8-sig")
    before = "private readonly TimeSpan _workerStartTimeout = TimeSpan.FromSeconds(30);"
    after = '''private readonly TimeSpan _workerStartTimeout = TimeSpan.FromSeconds(
            int.TryParse(Environment.GetEnvironmentVariable("GITHUB_ACTIONS_RUNNER_CHANNEL_TIMEOUT"), out int seconds)
                ? Math.Clamp(seconds, 30, 300) : 30);'''
    if text.count(before) != 1:
        raise ValueError("Pinned Worker startup timeout anchor differs")
    text = text.replace(before, after).replace(
        "// Wait for up to 30 seconds for a message from the channel.",
        '// Wait for the same startup timeout as the Listener.\n'
        '                    Trace.Info($"Worker startup IPC timeout: {_workerStartTimeout.TotalSeconds} seconds.");')
    path.write_text(text, encoding="utf-8")


def project(work, binpath):
    root = ET.Element("Project", Sdk="Microsoft.NET.Sdk")
    props = ET.SubElement(root, "PropertyGroup")
    values = {"TargetFramework": "net8.0", "OutputType": "Exe", "AssemblyName": "Runner.Worker",
              "Version": VERSION, "InformationalVersion": VERSION + "+nomercy.worker-startup." + PATCH_VERSION,
              "DefineConstants": "TRACE;OS_LINUX;X64", "NuGetAudit": "false", "DebugType": "none",
              "TreatWarningsAsErrors": "true", "EnableDefaultEmbeddedResourceItems": "false",
              "NoWarn": "SYSLIB0050;SYSLIB0051;CS0618", "PathMap": str(work) + "=/src/Runner.Worker"}
    for key, value in values.items():
        ET.SubElement(props, key).text = value
    items = ET.SubElement(root, "ItemGroup")
    refs = next(Path("/usr/share/dotnet/packs/Microsoft.NETCore.App.Ref").glob("8.*/ref/net8.0"))
    framework = {path.name for path in refs.glob("*.dll")}
    # These NuGet dependencies deliberately supersede the framework versions
    # in upstream's distribution. Reference those exact shipped versions.
    extras = {"System.Text.Json.dll", "System.Security.Cryptography.Pkcs.dll", "System.Threading.Channels.dll",
              "System.Security.Cryptography.ProtectedData.dll", "System.ServiceProcess.ServiceController.dll",
              "System.Diagnostics.DiagnosticSource.dll", "System.Text.Encodings.Web.dll", "System.Formats.Asn1.dll"}
    exclude = {"Runner.Worker.dll", "Runner.Listener.dll", "Runner.PluginHost.dll", "Runner.Plugins.dll"}
    for dll in sorted(binpath.glob("*.dll")):
        if ((dll.name in framework and dll.name not in extras)
                or dll.name.startswith("System.Private.") or dll.name in exclude):
            continue
        ref = ET.SubElement(items, "Reference", Include=dll.stem)
        ET.SubElement(ref, "HintPath").text = str(dll)
        ET.SubElement(ref, "Private").text = "false"
    resource = ET.SubElement(items, "EmbeddedResource", Include="action_yaml.json")
    ET.SubElement(resource, "LogicalName").text = "GitHub.Runner.Worker.action_yaml.json"
    ET.indent(root)
    ET.ElementTree(root).write(work / "Runner.Worker.csproj", encoding="unicode")


def build(archive, binpath, output, work):
    if sha256(archive) != SOURCE_SHA256 or sha256(binpath / "Runner.Worker.dll") != ORIGINAL_SHA256:
        raise ValueError("Source archive or original Worker differs from the pinned upstream release")
    work.mkdir(parents=True, exist_ok=False)
    source = work / "source"
    source.mkdir()
    with tarfile.open(archive) as tar:
        tar.extractall(source, filter="data")
    compile_dir = work / "compile"
    shutil.copytree(source / ("runner-" + VERSION) / "src/Runner.Worker", compile_dir)
    patch_worker(compile_dir / "Worker.cs")
    project(compile_dir, binpath)
    (compile_dir / "global.json").write_text(json.dumps({"sdk": {"version": SDK, "rollForward": "disable"}}))
    subprocess.run(["dotnet", "build", "-c", "Release", "--nologo", "--verbosity", "minimal",
                    "--source", str(work / "empty-nuget")], cwd=compile_dir, check=True, timeout=600)
    output.mkdir(parents=True, exist_ok=True)
    binary = output / "Runner.Worker.dll"
    shutil.copyfile(compile_dir / "bin/Release/net8.0/Runner.Worker.dll", binary)
    manifest = {"patch": "worker-startup", "patch_version": PATCH_VERSION, "upstream_version": VERSION,
                "upstream_commit": COMMIT, "source_sha256": SOURCE_SHA256, "original_sha256": ORIGINAL_SHA256,
                "worker_sha256": sha256(binary), "sdk": SDK}
    (output / "worker-startup.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("--bin-dir", type=Path, default=Path("/opt/actions-runner-image/bin"))
    parser.add_argument("--output", type=Path, default=Path("/out"))
    parser.add_argument("--work", type=Path, default=Path("/tmp/worker-startup-build"))
    args = parser.parse_args()
    build(args.archive, args.bin_dir, args.output, args.work)
