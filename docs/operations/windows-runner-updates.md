# Windows runner VM maintenance

Windows Update may automatically restart an unattended runner even when
`NoAutoRebootWithLoggedOnUsers=1`: that policy needs a logged-on user and the
appropriate scheduled-install policy. It is insufficient for five-day jobs.

The Windows runner VM uses explicit maintenance for OS updates. Run the
native agent Python as administrator **inside the enrolled guest**:

```powershell
& 'C:\ProgramData\nomercy\agent\python\python.exe' .\infra\windows\guest\set_runner_update_policy.py --worker rnr-windows-1
& 'C:\ProgramData\nomercy\agent\python\python.exe' .\infra\windows\guest\set_runner_update_policy.py --worker rnr-windows-1 --apply
```

The first command reads only. The second
requires an exact enrolled worker identity and recognized VM hardware, refuses
BEAST-UNIT, preserves the original registry values once in the agent directory
and proves the new policy by readback. A configured update deadline stops the
change for review. It does not reboot, stop a service or change host settings.
The guest installer requests this policy before starting its agent, so a newly
enrolled Windows guest receives it before accepting jobs. The common installer
does not apply it unless the guest-only option is explicitly supplied.

`NoAutoUpdate=1` keeps automatic Windows updates disabled; manual Windows Update
remains available. `AUOptions=2` records the notify mode if automatic updates are
later re-enabled. Defender is not disabled. Maintenance requires draining both
runners on the guest, proving no active jobs, installing updates deliberately,
then restarting only the guest if needed. Confirm both registrations and native
job events afterward before cancelling drain. Do not enforce installation or
restart deadlines that can interrupt an admitted long job.

Sources: [Configure Windows Update](https://learn.microsoft.com/en-us/windows/deployment/update/waas-wu-settings),
[Restart policy limitations](https://learn.microsoft.com/en-us/windows/deployment/update/waas-restart).
