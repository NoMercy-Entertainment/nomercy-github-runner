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


def test_the_verb_names_are_the_same_on_both_sides():
    """A verb the controller sends and the agent does not know is a 404 on a
    real worker; one the agent knows and the controller cannot send is dead
    code on the worker."""
    from control import agent_client
    assert agent_client.VERB_NAMES == agent_protocol().VERB_NAMES


def test_the_protocol_major_is_the_same_on_both_sides():
    from control import agent_client
    assert agent_client.PROTOCOL_MAJOR == agent_protocol().PROTOCOL_MAJOR


def test_the_request_path_is_the_same_on_both_sides():
    from control import agent_client
    assert agent_client.OP_PATH == agent_protocol().OP_PATH


def test_the_controllers_certificate_subject_is_the_same_on_both_sides():
    """The agent refuses any client whose certificate names something else.
    If the two disagreed, every call would be refused as an impostor."""
    from agent import tls
    from control import ca
    assert ca.CONTROLLER_SUBJECT == tls.CONTROLLER_SUBJECT
