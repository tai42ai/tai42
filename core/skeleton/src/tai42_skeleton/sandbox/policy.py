"""The ONE shared resolver of the operator-owned :class:`SandboxPolicy`.

Both the ``SandboxHolder.register_sandbox`` bind (which hands the policy to the kit
session-create chokepoint via ``bind_policy``) and the ``app.sandboxes.sandbox_policy()``
facade accessor / the ``sandbox_info()`` identity door read through THIS helper, so the
value the kit enforces and the value a plugin/door reads can never diverge. It resolves
the four platform-owned :class:`~tai42_skeleton.settings.settings.CoreSettings` knobs
(``sandbox_egress`` / ``sandbox_isolation`` / ``sandbox_scrub_transcript`` /
``sandbox_durable``) into one envelope. It is available REGARDLESS of whether a provider
is registered — the knobs live on ``CoreSettings``, which exist with or without a
provider.
"""

from __future__ import annotations

from tai42_contract.sandbox import SandboxPolicy

from tai42_skeleton.settings.settings import CoreSettings


def resolve_sandbox_policy() -> SandboxPolicy:
    """The resolved :class:`SandboxPolicy` from the four platform-owned settings knobs."""
    settings = CoreSettings()
    return SandboxPolicy(
        egress=settings.sandbox_egress,
        isolation=settings.sandbox_isolation,
        scrub_transcript=settings.sandbox_scrub_transcript,
        durable=settings.sandbox_durable,
    )
