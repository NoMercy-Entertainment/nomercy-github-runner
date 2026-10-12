"""Apply only to the reviewed v13.1.0 commit; never patch a moving tag."""
from pathlib import Path
import subprocess
import sys

COMMIT = "6095cb17bfdbded5aa4ea84c18a4b69fd9574cca"
source = Path(sys.argv[1]).resolve()
if subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() != COMMIT:
    raise SystemExit("Forgejo event patch requires the reviewed v13.1.0 commit")
path = source / "internal/pkg/report/reporter.go"
text = path.read_text(encoding="utf-8")
replacements = (
    ("type Reporter struct {", "type Reporter struct {\n\tlocalEvent *localJobEvent"),
    ("\treturn rv\n}", "\trv.localEvent = localEventFor(task)\n\tif err := rv.localEvent.emit(\"started\", nil); err != nil {\n\t\trv.log.Warn(\"local job start event could not be stored\")\n\t}\n\treturn rv\n}"),
    ("\tr.stateMu.Unlock()\n\n\treturn retry.Do(func() error {", "\tr.stateMu.Unlock()\n\n\tr.stateMu.RLock()\n\tresult := localResult(r.state.Result)\n\tr.stateMu.RUnlock()\n\tif err := r.localEvent.emit(\"finished\", result); err != nil {\n\t\tr.log.Warn(\"local job completion event could not be stored\")\n\t}\n\n\treturn retry.Do(func() error {"),
)
for before, after in replacements:
    if text.count(before) != 1:
        raise SystemExit("Forgejo source drifted from the reviewed patch anchors")
    text = text.replace(before, after)
path.write_text(text, encoding="utf-8", newline="\n")
for name in ("event.go", "event_test.go"):
    (path.parent / name).write_bytes((Path(__file__).parent / name).read_bytes())
