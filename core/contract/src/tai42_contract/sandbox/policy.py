"""The resolved sandbox security policy — a pure platform-policy envelope.

:class:`SandboxPolicy` is the security-as-config the kit enforces at every
session create. It is CONTRACT-defined (not kit-defined) precisely so the
:meth:`~tai42_contract.app.facets.AppSandboxes.sandbox_policy` accessor can
RETURN it and PLUGINS can IMPORT its type — the contract cannot import the kit
(layering: contract < kit < skeleton < plugins). The skeleton resolves ONE
``SandboxPolicy`` from its settings and binds the SAME value to the kit for
enforcement; the kit CONSUMES it and never defines a policy model of its own.
The ordering helpers ship beside the model so the kit imports them for the
network-ceiling and isolation-floor comparisons.
"""

from __future__ import annotations

from pydantic import BaseModel

from tai42_contract.sandbox.models import SandboxIsolation, SandboxNetwork

# Network openness ordered weakest→strongest; the index into this tuple is a
# tier's openness rank, so a spec whose ``network`` outranks ``policy.egress`` is
# looser than the ceiling.
_NETWORK_ORDER: tuple[SandboxNetwork, ...] = ("none", "internal", "egress")

# Isolation strength ordered weakest→strongest; the index is a tier's strength
# rank, so a spec whose ``isolation`` ranks below ``policy.isolation`` is under
# the floor.
_ISOLATION_ORDER: tuple[SandboxIsolation, ...] = ("none", "container", "vm")


def network_openness(network: SandboxNetwork) -> int:
    """The openness rank of a network tier (``none`` < ``internal`` < ``egress``)."""
    return _NETWORK_ORDER.index(network)


def isolation_strength(isolation: SandboxIsolation) -> int:
    """The strength rank of an isolation tier (``none`` < ``container`` < ``vm``)."""
    return _ISOLATION_ORDER.index(isolation)


class SandboxPolicy(BaseModel):
    """The resolved security policy the kit enforces at session create.

    ``egress`` is the network CEILING (a session's ``network`` must be
    at-or-tighter); ``isolation`` is the strength FLOOR (a session runs at
    at-least this level); ``durable`` gates whether a ``persistent`` session is
    permitted at all; ``scrub_transcript`` is carried for the consumer to read —
    it is applied consumer-side, NOT a create-time gate. It is a pure
    platform-policy envelope — no consumer concept lives here.
    """

    egress: SandboxNetwork
    isolation: SandboxIsolation
    scrub_transcript: bool
    durable: bool
