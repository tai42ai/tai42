"""The shared driving context manager the park/caller/door probe tools drive an ``ask`` under.

A driving probe binds a resume continuation tool around an ``ask`` that parks, so the park stores
that tool as the run's resumer; a stand-in flow driver also binds a synthetic execution identity
the park stores as its ``continuation_identity`` so a resume that rebinds it is provable.
``driving_as`` binds the continuation tool and, when the caller supplies one, the synthetic
identity, and resets both when the block exits so a driver's binding never leaks past its ``ask``.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from tai42_skeleton.authz.identity import CallerIdentity


@contextmanager
def driving_as(*, continuation: str, identity: CallerIdentity | None = None) -> Iterator[None]:
    """Bind ``continuation`` as the run's resume continuation tool for the duration of the block,
    optionally binding ``identity`` as the execution identity the park stores, resetting both on exit.

    Pass ``identity`` to bind a synthetic execution identity (a stand-in flow driver that owns its
    identity, or a consumer that has decided to bind one because the stack has none); leave it
    ``None`` to drive under whatever identity is already bound (an agent turn's route execution
    key, or none on an auth-off stack). Both tokens are reset in ``finally``, so the binding is
    scoped to the ``ask`` and never leaks to the caller.
    """
    from tai42_contract.interactions import reset_resume_continuation_tool, set_resume_continuation_tool
    from tai42_skeleton.authz.execution_identity import reset_execution_identity, set_execution_identity

    tool_token = set_resume_continuation_tool(continuation)
    identity_token = set_execution_identity(identity) if identity is not None else None
    try:
        yield
    finally:
        if identity_token is not None:
            reset_execution_identity(identity_token)
        reset_resume_continuation_tool(tool_token)
