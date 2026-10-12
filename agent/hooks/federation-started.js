"use strict";
// This extra hook belongs only to privately claimed lent runners. Normal
// runner hook files and trusted-owner policy remain unchanged.
const fs = require("fs");
const path = require("path");
const child = require("child_process");

function allowed(event, payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
  if (payload.pull_request !== undefined || /^pull_request/.test(event)) {
    const head = payload.pull_request?.head?.repo;
    const base = payload.pull_request?.base?.repo;
    return Number.isSafeInteger(head?.id) && head.id > 0 && head.id === base?.id && head.fork === false;
  }
  if (event === "workflow_run") {
    const run = payload.workflow_run;
    return Array.isArray(run?.pull_requests) && run.pull_requests.length === 0 &&
      Number.isSafeInteger(run?.head_repository?.id) && run.head_repository.id === payload.repository?.id &&
      run.head_repository.fork === false;
  }
  return ["push", "workflow_dispatch", "schedule", "repository_dispatch", "release", "create", "delete"].includes(event) &&
    Number.isSafeInteger(payload.repository?.id) && payload.repository.id > 0 && payload.repository.fork === false;
}

function processes() {
  if (process.platform === "win32") {
    const program = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe";
    const query = "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,CreationDate | ConvertTo-Json -Compress";
    const rows = JSON.parse(child.execFileSync(program, ["-NoProfile", "-NonInteractive", "-Command", query],
      {encoding: "utf8", timeout: 15000, maxBuffer: 1048576}));
    return new Map(rows.map(row => [row.ProcessId, {parent: row.ParentProcessId, name: row.Name, born: row.CreationDate}]));
  }
  const raw = child.execFileSync("/bin/ps", ["-axo", "pid=,ppid=,lstart=,comm="],
    {encoding: "utf8", timeout: 5000, maxBuffer: 1048576});
  const rows = new Map();
  for (const line of raw.split("\n")) {
    const match = /^\s*(\d+)\s+(\d+)\s+(.{24})\s+(.+)$/.exec(line);
    if (match) rows.set(Number(match[1]), {parent: Number(match[2]), born: match[3], name: path.basename(match[4])});
  }
  return rows;
}

function endWorker() {
  const rows = processes();
  let pid = process.ppid;
  for (let depth = 0; depth < 64 && pid > 1; depth++) {
    const row = rows.get(pid);
    if (!row) break;
    if (["Runner.Worker", "Runner.Worker.exe"].includes(row.name)) {
      // Fence PID reuse: the same parent chain, name and creation time must
      // still identify the job worker, never its listener or another process.
      const current = processes();
      let parent = process.ppid;
      for (let check = 0; check < 64 && parent > 1; check++) {
        const entry = current.get(parent);
        if (parent === pid && entry?.name === row.name && entry?.born === row.born) {
          process.kill(pid, "SIGKILL");
          return true;
        }
        if (!entry) break;
        parent = entry.parent;
      }
      return false;
    }
    pid = row.parent;
  }
  return false;
}

function main() {
  let safe = false;
  try {
    const file = process.env.GITHUB_EVENT_PATH;
    if (fs.statSync(file).size <= 1048576) safe = allowed(process.env.GITHUB_EVENT_NAME || "", JSON.parse(fs.readFileSync(file, "utf8")));
  } catch (_) { /* Unreadable events are refused for lent capacity. */ }
  if (!safe) {
    process.stdout.write("::error title=Lent runner::Forked or unverified job events are refused on lent capacity\n");
    try { if (!endWorker()) process.stdout.write("::error title=Lent runner::The job worker could not be safely identified\n"); }
    catch (_) { process.stdout.write("::error title=Lent runner::The job worker could not be stopped\n"); }
    process.exitCode = 1;
    return;
  }
  if (process.platform === "win32") require(path.join(__dirname, "job-started.js"));
  else {
    const result = child.spawnSync("/bin/bash", [path.join(__dirname, "job-started.sh")], {stdio: "inherit"});
    process.exitCode = result.error || result.status === null ? 1 : result.status;
  }
}

module.exports = {allowed, endWorker};
if (require.main === module) main();
