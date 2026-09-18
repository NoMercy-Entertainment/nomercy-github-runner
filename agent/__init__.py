"""The control agent: what runs on a worker and does what the controller asks.

A separate deployable from the dashboard. It runs on each worker - a Hyper-V
Linux VM, a Windows Server guest - and it imports nothing from `dashboard/`, so
it can be installed without it. The two share a vocabulary, not code: verb
names, probe names and the protocol version live in `agent/protocol.py`, and a
test on the dashboard side reads that file and fails if the controller's copy
disagrees.

**It can only be asked for named things.** `verbs.VERBS` is a frozen table of
fourteen verbs. There is no verb that runs a command, takes a shell string, or
names a path outside one runner's own tree, and a test scans this package's
source for process-spawning calls whose arguments are anything but a literal
list. That is design NFR-10, enforced rather than intended: the agent listens
on the network with the power to create and destroy runners, and the one thing
worse than a bug in it would be a way to make it run something else.
"""
