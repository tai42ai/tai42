"""The ``TaiMCP`` lifecycle mixin, composed from a construction/spine base and per-concern mixins.

Also holds the module-global seam the tests patch at this package path.
"""

from tai42_contract.access_control.registry import reset_registry as reset_identity_registry
from tai42_kit.clients import shutdown_all_clients
from tai42_kit.clients.impl.mcp import FastMCPClient
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.store.store_registry import store_registry

from tai42_skeleton.app.epoch import is_epoch_rebuild_in_progress
from tai42_skeleton.app.importer import import_or_reload_package
from tai42_skeleton.app.kind_status import collect_kind_status
from tai42_skeleton.app.lifecycle.boot import BootMixin
from tai42_skeleton.app.lifecycle.boot_manifest import BootManifestMixin
from tai42_skeleton.app.lifecycle.bus_subscription import BusSubscriptionMixin
from tai42_skeleton.app.lifecycle.component_import import ComponentImportMixin
from tai42_skeleton.app.lifecycle.config_reload import ConfigReloadMixin
from tai42_skeleton.app.lifecycle.handlers import LifecycleHandlersMixin
from tai42_skeleton.app.lifecycle.lifespan import LifespanMixin
from tai42_skeleton.app.lifecycle.mcp_probe import McpProbeMixin
from tai42_skeleton.app.lifecycle.mcp_reload import McpReloadMixin
from tai42_skeleton.app.lifecycle.mcp_reprobe import McpReprobeMixin
from tai42_skeleton.app.lifecycle.router_modules import RouterModulesMixin
from tai42_skeleton.app.lifecycle.serving_core_access import ServingCoreAccessMixin
from tai42_skeleton.app.lifecycle.state import LifecycleState
from tai42_skeleton.app.route_registry import route_registry
from tai42_skeleton.monitoring import get_monitoring
from tai42_skeleton.settings.settings import CoreSettings

# The module-global seam: every symbol a test patches at
# ``tai42_skeleton.app.lifecycle.<name>`` lives here as a package attribute, and the
# concern mixins read it through this package namespace at call time (``_lifecycle.<name>``),
# so a package-level ``monkeypatch.setattr`` takes effect with no per-submodule copy.
__all__ = [
    "CoreSettings",
    "FastMCPClient",
    "TaiMCPLifecycleMixin",
    "checkpoint_registry",
    "collect_kind_status",
    "get_monitoring",
    "import_or_reload_package",
    "is_epoch_rebuild_in_progress",
    "reset_identity_registry",
    "route_registry",
    "shutdown_all_clients",
    "store_registry",
]


class TaiMCPLifecycleMixin(
    ServingCoreAccessMixin,
    LifecycleHandlersMixin,
    BootManifestMixin,
    LifespanMixin,
    BusSubscriptionMixin,
    BootMixin,
    RouterModulesMixin,
    ComponentImportMixin,
    McpProbeMixin,
    McpReloadMixin,
    McpReprobeMixin,
    ConfigReloadMixin,
    LifecycleState,
):
    """The concrete app's lifecycle surface, composed from the per-concern mixins over the spine base.

    It carries no body of its own: construction is ``LifecycleState.__init__`` and
    every method comes from one owning mixin. Consumed by ``TaiMCP``
    (``app/server.py``) and the test doubles; ``_mcp_tools`` stays abstract for the
    concrete subclass to implement.
    """
