"""Backend registration — the impl body behind the ``app.backends`` facet."""

from tai42_contract.backend import Backend

from tai42_skeleton.settings.cache import backend_provider


class BackendHolder:
    """Holds the process's single registered :class:`Backend` instance."""

    def __init__(self) -> None:
        """Start with no backend registered."""
        self._backend: Backend | None = None

    @property
    def backend(self) -> Backend | None:
        """The registered backend instance, or ``None`` when none has been registered."""
        return self._backend

    def register_backend(self, cls: type | None = None):
        """Register a backend class, instantiating it; usable directly or as a decorator."""
        if cls:
            return self.register_backend()(cls)

        def decorator(klass):
            self._backend = klass()
            return klass

        return decorator

    async def launch(self, args) -> None:
        """Launch the registered backend with ``args``, raising when none is configured."""
        if self._backend is None:
            raise RuntimeError(f"Backend provider {backend_provider()} is not configured")
        await self._backend.launch(args)
