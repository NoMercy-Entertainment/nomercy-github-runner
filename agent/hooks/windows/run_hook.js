// What job-started.js and job-completed.js share: start runner_disk.py, beside
// this file, with the agent's own Python (RUNNER_HOOK_PYTHON), and turn its
// status into the hook's.
//
// The runner runs a .js hook under its own bundled node: no execution policy
// to refuse it, as the ARM64 guest's does a .ps1, and no PowerShell to start,
// which takes minutes under emulation. Only node's own modules are used.
//
// Exit status: 1 only when the check refuses the job on purpose (75 from a
// job-started check). Anything else - no Python, a Python that cannot start,
// a crash, any other status - is a ::warning and lets the job run.
"use strict";
const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const REFUSE = 75;

function warn(text) {
  try {
    process.stdout.write(`::warning title=Runner hook::${text}\n`);
  } catch (error) {
    // Nowhere left to say it; the job still runs.
  }
}

function run(stage, what) {
  try {
    const python = process.env.RUNNER_HOOK_PYTHON;
    if (!python) {
      warn(`${what} did not run: RUNNER_HOOK_PYTHON names no Python`);
      return 0;
    }
    // A Python that is not there is spawnSync's ENOENT, below. existsSync
    // would also refuse an app execution alias that runs perfectly well.
    const script = path.join(__dirname, "runner_disk.py");
    if (!fs.existsSync(script)) {
      warn(`${what} did not run: ${script} is missing`);
      return 0;
    }
    // -X utf8: the log gets UTF-8 whatever the console's code page is. -I
    // ignores PYTHON* variables, so PYTHONIOENCODING would not reach it.
    const result = spawnSync(python, ["-I", "-B", "-X", "utf8", script, stage], {
      stdio: ["ignore", "inherit", "inherit"],
      windowsHide: true,
    });
    if (result.error) {
      warn(`${what} could not start: ${result.error.message}`);
      return 0;
    }
    if (stage === "started" && result.status === REFUSE) {
      return 1;
    }
    if (result.status !== 0) {
      const status = result.status === null ? result.signal : result.status;
      warn(`${what} ended with status ${status}`);
    }
    return 0;
  } catch (error) {
    warn(`${what} failed: ${error && error.message}`);
    return 0;
  }
}

module.exports = { run };
