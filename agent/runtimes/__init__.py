"""The runtimes an agent can drive: one per kind of execution unit.

Each implements the agent's `Runtime` and `Registrar` protocols (agent/verbs.py)
and knows nothing about which forge its runners belong to. `linux_container`
runs a runner as a container with five named volumes; `windows_process` runs
one as a service under its own virtual account, in a Job Object, over its own
directory tree. The contract suite in agent/tests/contract was written before
the second of them, so each is held to the contract rather than the contract
bent to them.
"""
