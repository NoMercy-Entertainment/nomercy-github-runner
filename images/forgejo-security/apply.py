"""Apply bounded upstream security corrections to the reviewed Forgejo commit."""
from pathlib import Path
import subprocess
import sys

source = Path(sys.argv[1]).resolve()
commit = "6095cb17bfdbded5aa4ea84c18a4b69fd9574cca"
if subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() != commit:
    raise SystemExit("Security overlay requires the reviewed Forgejo commit")

def replace(relative, before, after):
    file = source / relative
    text = file.read_text()
    if text.count(before) != 1:
        raise SystemExit("Security patch anchor differs: " + relative)
    file.write_text(text.replace(before, after), encoding="utf-8", newline="\n")

replace("act/common/git/git.go",
    r"`^https?://git-codecommit\.(.+)\.amazonaws.com/v1/repos/(.+)$`",
    r"`^https?://git-codecommit\.([a-z0-9-]+)\.amazonaws\.com/v1/repos/([^/?#]+)$`")
replace("act/common/git/git.go",
    r"`ssh://git-codecommit\.(.+)\.amazonaws.com/v1/repos/(.+)$`",
    r"`^ssh://git-codecommit\.([a-z0-9-]+)\.amazonaws\.com/v1/repos/([^/?#]+)$`")
replace("act/common/git/git.go",
    r"`^https?://.*github.com.*/(.+)/(.+?)(?:.git)?$`",
    r"`^https?://github\.com/([^/?#]+)/([^/?#]+?)(?:\.git)?$`")
replace("act/common/git/git.go",
    r"`github.com[:/](.+)/(.+?)(?:.git)?$`",
    r"`^(?:(?:git\+)?ssh://)?(?:[^/@:]+@)?github\.com[:/]([^/?#]+)/([^/?#]+?)(?:\.git)?$`")

replace("act/filecollector/file_collector.go",
    """\tfdestpath := filepath.Join(cc.DstDir, fpath)
\tif err := os.MkdirAll(filepath.Dir(fdestpath), 0o777); err != nil {
\t\treturn err
\t}
\tif linkName != "" {
\t\treturn os.Symlink(linkName, fdestpath)
\t}

\tdf, err := os.OpenFile(fdestpath, os.O_CREATE|os.O_WRONLY, fi.Mode())""",
    """\tif !filepath.IsLocal(fpath) {
\t\treturn fmt.Errorf("archive entry is outside the destination")
\t}
\tif linkName != "" && (filepath.IsAbs(linkName) ||
\t\t!filepath.IsLocal(filepath.Join(filepath.Dir(fpath), linkName))) {
\t\treturn fmt.Errorf("archive link is outside the destination")
\t}
\tif err := os.MkdirAll(cc.DstDir, 0o777); err != nil {
\t\treturn err
\t}
\troot, err := os.OpenRoot(cc.DstDir)
\tif err != nil {
\t\treturn err
\t}
\tdefer root.Close()
\tif err := root.MkdirAll(filepath.Dir(fpath), 0o777); err != nil {
\t\treturn err
\t}
\tif linkName != "" {
\t\treturn root.Symlink(linkName, fpath)
\t}

\tdf, err := root.OpenFile(fpath, os.O_CREATE|os.O_WRONLY, fi.Mode().Perm())""")

replace("act/runner/step.go",
    'if strings.HasPrefix(dest, prefix) || prefix == "./" {',
    'if !path.IsAbs(sym) && (strings.HasPrefix(dest, prefix) || (prefix == "./" && filepath.IsLocal(dest))) {')

tests = {"filecollector": "copy_security_test.go", "common/git": "hostname_security_test.go",
         "runner": "symlink_security_test.go"}
for package, name in tests.items():
    (source / "act" / package / name).write_bytes((Path(__file__).parent / name).read_bytes())
