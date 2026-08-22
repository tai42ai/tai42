"""Sandbox identity + the resolved-policy surface.

``sandbox_info`` reports whether a :class:`~tai42_contract.sandbox.Sandbox` provider is
registered and, alongside that identity, the RESOLVED security-as-config policy the kit
enforces at every session create. Mirrors ``operations/backend.py``'s ``backend_info``:
``present: false`` never raises, and the door is the HTTP/operator/e2e surface that
SURFACES the resolved policy to EXTERNAL consumers (an in-process plugin reads the same
policy through the ``app.sandboxes.sandbox_policy()`` facade accessor, not this door).

The ``policy`` sub-object reads the SAME resolved policy the ``sandbox_policy()`` accessor
returns — through the ONE shared skeleton resolver — so the door and the accessor never
disagree. It is PRESENT REGARDLESS of whether a provider is registered: the policy is
resolved from ``CoreSettings``, which exist whether or not a provider is installed. It
carries NO consumer concept — the platform's own resolved knobs only.
"""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_skeleton.operations import operation


@operation(summary="Get the sandbox identity and resolved policy", tags=["sandbox"])
async def sandbox_info() -> dict:
    from tai42_skeleton.sandbox.policy import resolve_sandbox_policy

    policy = resolve_sandbox_policy()
    policy_view = {
        "egress": policy.egress,
        "isolation": policy.isolation,
        "scrub_transcript": policy.scrub_transcript,
        "durable": policy.durable,
    }
    sandbox = tai42_app.sandboxes.sandbox
    if sandbox is None:
        return {"present": False, "provider": None, "module": None, "sessions": 0, "policy": policy_view}
    cls = type(sandbox)
    sessions = len(await sandbox.list_sessions())
    return {
        "present": True,
        "provider": cls.__qualname__,
        "module": cls.__module__,
        "sessions": sessions,
        "policy": policy_view,
    }
