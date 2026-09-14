"""The public facet class behind ``app.tools``, composing the binding concerns."""

from tai42_skeleton.tools.binding.client_tools import _ClientToolsMixin
from tai42_skeleton.tools.binding.dispatch import _DispatchMixin
from tai42_skeleton.tools.binding.registration import _RegistrationMixin


class ToolBinding(_DispatchMixin, _RegistrationMixin, _ClientToolsMixin):
    """Binds tools onto the app's live FastMCP server.

    Holds no lifecycle state of its own — manifest/registries/server are
    properties over the owning app, which rebuilds them on every start/reload.
    """
