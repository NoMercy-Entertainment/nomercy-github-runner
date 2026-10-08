# A GitHub runner removes itself with a removal token minted from the org's
# own token, and a unit is never given that (design 18.3). So this says so,
# with a status the agent reports as a failure, and the controller deletes
# the record at GitHub by its id (control/provision.py). Exiting 0 would be
# the lie that strands a record: the controller would take the runner as
# deregistered.
#
# The same answer the Linux unit's `deregister` gives, for the same reason.
$agent = Join-Path $PSScriptRoot 'agent'
if (-not (Test-Path -LiteralPath (Join-Path $agent '.runner'))) {
    [Console]::Error.WriteLine('not registered: nothing to remove at the forge')
    exit 0
}
[Console]::Error.WriteLine('a GitHub unit holds no credential that can remove its runner; the controller deletes the record at the forge by its id')
exit 3
