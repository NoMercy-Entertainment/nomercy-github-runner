// The GitHub runner's job-completed hook on Windows. The name ends in .js
// because the runner runs only .js, .sh and .ps1 hooks; any other path fails
// the job's last step, and the job with it. A .js one runs under the
// runner's own node. The work is in runner_disk.py, started by run_hook.js.
// It never fails a job.
"use strict";
try {
  const path = require("path");
  require(path.join(__dirname, "run_hook.js")).run("completed", "job-completed cleanup");
} catch (error) {
  try {
    process.stdout.write(`::warning title=Runner hook::job-completed cleanup failed: ${error && error.message}\n`);
  } catch (ignored) {
    // The job still runs.
  }
}
process.exitCode = 0;
