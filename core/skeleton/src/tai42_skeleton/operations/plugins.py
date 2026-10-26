"""Studio-plugin registry operation — list the installed plugins' manifest contents.

The listing reads the startup-built registry.
The plugin asset-serving and SPA-hosting routes are content servers (they answer
file bytes / injected HTML with per-response security headers, not the
``{"data": ...}`` envelope), so they stay handlers in the router. Only the
registry listing is an enveloped-JSON read, so it lives here as an operation. A
registry that has not been built is a loud 500 (:class:`OperationFailedError`) rather
than a fabricated empty list.
"""

from __future__ import annotations

from tai42_skeleton.app.mount_map import package_mounts
from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.operations import OperationFailedError, operation
from tai42_skeleton.operations.response_models.plugins import StudioPluginListing
from tai42_skeleton.plugins.registry import StudioPluginError, current_registry


@operation(
    summary="List the registered studio plugins",
    tags=["plugins"],
    errors=[OperationFailedError],
    response_model=StudioPluginListing,
)
async def list_studio_plugins() -> list:
    """Every registered studio plugin's manifest contents and route mounts, or a loud 500 if the registry is unbuilt.

    ``mounts`` is read off the committed route generation, so a remapped or dropped route item reads as it is
    served now.
    """
    try:
        registry = current_registry()
    except StudioPluginError as exc:
        raise OperationFailedError(str(exc)) from exc
    live = route_registry.live_routes()
    return [{**manifest, "mounts": package_mounts(manifest["name"], live)} for manifest in registry.manifest_contents()]
