"""The controller and the agent use the same words.

They are separate deployables - the agent is installed on workers without the
dashboard, and the dashboard image does not ship the agent - so each carries its
own copy of the protocol's vocabulary. This file is what makes two copies safe:
it reads the agent's copy and fails on any difference. Extended in T-0402 with
the verb names and protocol version the controller's client uses.
"""
import os
import sys

from runtime.base import Probe

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def agent_protocol():
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    from agent import protocol
    return protocol


def test_the_probe_names_are_the_same_on_both_sides():
    """A probe the controller can ask for and the agent refuses, or the other
    way round, would fail only at run time on a real worker."""
    assert {p.value for p in Probe} == agent_protocol().PROBES
