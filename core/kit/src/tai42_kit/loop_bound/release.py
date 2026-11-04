"""The one release for everything the kit keeps for the running event loop."""

from tai42_kit.clients.base import _shutdown_all_clients
from tai42_kit.loop_bound.registry import _release_registries


async def release_loop_bound_resources() -> None:
    """Release everything the kit keeps for the running event loop, every epoch of it.

    Its loop-bound resource registries are closed, then its pooled clients. Registries go first, so
    a resource built on a pooled client closes before the client. Each step runs whatever the other
    raised; one failure is raised as itself, several as
    ``ExceptionGroup("releasing the loop's kit resources failed", ...)``.

    A release does not end the loop's leasing: on a loop that lives on, the next lease builds its
    pool or registry anew and the loop's next release closes it.
    :meth:`~tai42_kit.backend.PrivateLoop.close` marks its loop closing first
    (:mod:`tai42_kit.loop_closing`), so a lease after its release is refused instead of opening
    what nothing would release. A loop not marked (one ``asyncio.run`` ends) refuses nothing: a
    lease on it after its last release opens a resource that stays open.
    """
    errors: list[Exception] = []
    for step in (_release_registries, _shutdown_all_clients):
        try:
            await step()
        except Exception as exc:
            errors.append(exc)
    if len(errors) == 1:
        raise errors[0]
    if errors:
        raise ExceptionGroup("releasing the loop's kit resources failed", errors)
