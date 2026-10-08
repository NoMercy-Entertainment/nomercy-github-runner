// The GitHub runner's job-started hook on Windows. The name ends in .js
// because the runner runs only .js, .sh and .ps1 hooks, and a .js one under
// its own node, which nothing on the guest can refuse to run. The work is in
// runner_disk.py, started by run_hook.js. Only its deliberate refusals (a pull
// request from an outside fork, a disk too full for the job) fail the job;
// anything else lets the job run.
"use strict";
let code = 0;
try {
  const path = require("path");
  code = require(path.join(__dirname, "run_hook.js")).run("started", "job-started check");
} catch (error) {
  try {
    process.stdout.write(`::warning title=Runner hook::job-started check failed: ${error && error.message}\n`);
  } catch (ignored) {
    // The job still runs.
  }
  code = 0;
}
process.exitCode = code;
