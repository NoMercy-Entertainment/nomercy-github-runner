# forgejo-runner has no unregister. This says so, with a status the agent
# reports as a failure, and the controller deletes the record at Forgejo by
# its id (control/provision.py). Exiting 0 would be the lie that strands a
# record: the controller would take the runner as deregistered.
if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.runner'))) {
    [Console]::Error.WriteLine('not registered: nothing to remove at the forge')
    exit 0
}
[Console]::Error.WriteLine('forgejo-runner cannot remove itself; the controller deletes the record at the forge by its id')
exit 3
