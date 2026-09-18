"""The runtimes an agent can drive: one per kind of execution unit.

Each implements the agent's `Runtime` and `Registrar` protocols (agent/verbs.py)
and knows nothing about which forge its runners belong to. Linux containers are
here now; the Windows process runtime and the macOS appliance arrive in phases
6 and 7, and the contract suite in agent/tests/contract is written before them
so they are held to the contract rather than the contract bent to them.
"""
