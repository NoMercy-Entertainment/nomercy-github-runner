"""Runtime adapters: the seam between the controller and one kind of
execution unit.

The controller knows lifecycle verbs. It does not know what a container is,
what a Windows service is, or what a QEMU appliance is. Each of those is a
runtime adapter implementing the same contract, chosen from the RunnerSpec by
table lookup rather than by a conditional at a call site.

See docs/superpowers/specs/2026-09-17-uniform-hyperv-runner-platform-design.md
sections 10.3 and 10.4.
"""
