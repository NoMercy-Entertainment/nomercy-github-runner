"""A real agent on localhost, reached over real mutual TLS.

Shared by every controller-to-agent test from T-0402 on. Nothing here is a
stand-in for TLS: certificates are minted by the control plane's authority,
the agent is the real server, and the controller is the real client. Only the
agent's runtime and registrar are fakes, because phase 4 has not built the real
ones yet.
"""
import os
import sys

from control import agent_client as ac
from control import ca
from control.agent_client import AgentClient
from control.inventory import HYPERV_LINUX, Inventory

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent import tls as agent_tls                        # noqa: E402
from agent.server import AgentServer                      # noqa: E402
from agent.tests.fakes import FakeRegistrar, FakeRuntime  # noqa: E402
from agent.verbs import Agent                             # noqa: E402

RID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


class PKI:
    """A control-plane authority, and a place to write what it issues."""

    def __init__(self, directory):
        self.dir = directory
        self.ca_cert, self.ca_key = ca.create_ca()
        self.ca_file = self.write("ca.pem", self.ca_cert)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def issue(self, subject, role, name=None, authority=None):
        """Returns (cert file, key file, cert PEM)."""
        cert_pem, key_pem = ca.issue(*(authority or (self.ca_cert,
                                                     self.ca_key)),
                                     subject, role)
        name = name or subject
        return (self.write(f"{name}.crt", cert_pem),
                self.write(f"{name}.key", key_pem), cert_pem)


def serve(pki, subject="linux-1", name=None, authority=None, permitted=None,
          server_class=AgentServer, **server_kwargs):
    """An agent with a certificate for `subject`. Returns (server, runtime,
    registrar, cert PEM)."""
    cert, key, pem = pki.issue(subject, "agent", name=name,
                               authority=authority)
    runtime, registrar = FakeRuntime(), FakeRegistrar()
    server = server_class(
        Agent(subject, runtime, registrar, permitted=permitted),
        ssl_context=agent_tls.server_context(cert, key, pki.ca_file),
        **server_kwargs).start()
    return server, runtime, registrar, pem


def controller(pki, db, subject=ca.CONTROLLER_SUBJECT, authority=None,
               name="controller", timeout=5):
    cert, key, _ = pki.issue(subject, "controller", name=name,
                             authority=authority)
    return AgentClient(Inventory(db), cert, key, pki.ca_file,
                       audit_path=db, timeout=timeout)


def enrol(db, host_id, server, pem, scheme="https", verbs=None):
    """Register a worker at the server's address, pinned to the certificate it
    was issued, and permitted `verbs` - every verb unless told otherwise.
    Enrolment is deliberate: nothing here trusts a worker on first contact."""
    inventory = Inventory(db)
    inventory.register_worker(
        host_id, HYPERV_LINUX,
        endpoint=f"{scheme}://127.0.0.1:{server.port}",
        certificate_fingerprint=ca.fingerprint(pem) if pem else None)
    inventory.permit(host_id, ac.VERB_NAMES if verbs is None else verbs)
    return inventory
