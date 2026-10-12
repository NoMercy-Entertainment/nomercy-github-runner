"""Prepare admission source only against the reviewed Forgejo runner commit.

This helper is deliberately separate from the existing release build/defaults.
It neither compiles nor installs a runner and supplies no live policy IDs.
"""
from pathlib import Path
import subprocess
import sys

COMMIT = "6095cb17bfdbded5aa4ea84c18a4b69fd9574cca"
source = Path(sys.argv[1]).resolve()
if subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip() != COMMIT:
    raise SystemExit("Forgejo admission patch requires the reviewed v13.1.0 commit")
path = source / "internal/app/run/runner.go"
text = path.read_text(encoding="utf-8")
replacements = (
    ('\t"maps"\n', '\t"maps"\n\t"os"\n'),
    ("\trunningTasks sync.Map\n", "\trunningTasks sync.Map\n\tadmission *admissionPolicy\n\tadmissionErr error\n"),
    ("\treturn &Runner{\n", "\tpolicy, policyErr := loadAdmissionPolicy(os.Getenv(admissionPolicyEnvironment), cli.Address())\n\treturn &Runner{\n\t\tadmission: policy,\n\t\tadmissionErr: policyErr,\n"),
    ("func (r *Runner) Run(ctx context.Context, task *runnerv1.Task) {\n", "func (r *Runner) Run(ctx context.Context, task *runnerv1.Task) {\n\ttask = admissionReportableTask(task)\n\tif task == nil {\n\t\tlog.Error(\"admission refused a missing task\")\n\t\treturn\n\t}\n"),
    ('\treporter.Logf("Runner %s (version:%s) received task', '\tif err := r.admit(task); err != nil {\n\t\treporter.Logf("Runner admission refused this task: %s", err)\n\t\treturn err\n\t}\n\n\treporter.Logf("Runner %s (version:%s) received task'),
)
for before, after in replacements:
    if text.count(before) != 1:
        raise SystemExit("Forgejo source drifted from the reviewed admission anchors")
    text = text.replace(before, after)
path.write_text(text, encoding="utf-8", newline="\n")
for name in ("admission.go", "admission_test.go"):
    (path.parent / name).write_bytes((Path(__file__).parent / name).read_bytes())
