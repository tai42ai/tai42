"""What the kit keeps per event loop, and the release that closes it.

Vendor-free: the registry machinery here needs only the standard library and the kit's
client epoch. The end of a loop, and an application's shutdown on a loop that lives on, await
:func:`release_loop_bound_resources`; :meth:`~tai42_kit.backend.PrivateLoop.close` first marks
its loop closing (:mod:`tai42_kit.loop_closing`), and a loop not marked refuses no lease after a
release. A generation's retire on the serving loop calls
:func:`close_retired_registries` for the retired epoch.
"""

from tai42_kit.loop_bound.registry import (
    LoopRegistryMap,
    ResourceRegistry,
    close_retired_registries,
)
from tai42_kit.loop_bound.release import release_loop_bound_resources

__all__ = [
    "LoopRegistryMap",
    "ResourceRegistry",
    "close_retired_registries",
    "release_loop_bound_resources",
]
