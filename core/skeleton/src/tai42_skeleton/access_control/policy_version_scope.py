"""The request-scoped memo of the policy version.

One access-control decision reads the policy version at several layers (the auth backend's
standing, the resource guard's route resolution). Inside an open scope the first successful
read is remembered and every later read of the same decision answers from it, so the
decision costs one Redis round trip and every layer decides on the same version.

The scope is opened by the access-control middleware stack before authentication and closed
by the resource guard as it hands the request to the app: everything the endpoint runs (tool
dispatches, agent turns, a long-lived MCP/SSE session's later work) reads the version live,
so a policy change is never hidden behind a request that stays open. Code with no scope bound
(background workers, schedules, hooks) reads live on every call.

Only a successful read is remembered; a read that raises leaves the memo empty and the next
read goes to Redis again.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class _VersionMemo:
    value: int | None = None
    closed: bool = False


_SCOPE: ContextVar[_VersionMemo | None] = ContextVar("policy_version_scope", default=None)


@contextmanager
def policy_version_scope() -> Iterator[None]:
    """Bind a fresh, open version memo for the block and unbind it on exit."""
    token = _SCOPE.set(_VersionMemo())
    try:
        yield
    finally:
        _SCOPE.reset(token)


def close_policy_version_scope() -> None:
    """Close the bound memo: every later read in this scope goes to Redis. A no-op when none is bound."""
    memo = _SCOPE.get()
    if memo is not None:
        memo.closed = True


def _open_memo() -> _VersionMemo | None:
    memo = _SCOPE.get()
    if memo is None or memo.closed:
        return None
    return memo


def memoized_policy_version() -> int | None:
    """The version an open scope already holds, or ``None`` when there is no open scope or nothing read yet."""
    memo = _open_memo()
    return None if memo is None else memo.value


def remember_policy_version(version: int) -> None:
    """Store ``version`` in the open scope's memo. A no-op when no scope is bound or the scope is closed."""
    memo = _open_memo()
    if memo is not None:
        memo.value = version
