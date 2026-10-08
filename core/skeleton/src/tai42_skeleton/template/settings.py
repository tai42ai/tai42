"""Cache settings for :class:`ResourceManager`, co-located with the impl.

Composes the kit settings machinery (:class:`tai42_kit.settings.TaiBaseSettings`
plus the ``settings_cache`` reset registry).
"""

from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache


class FileLoadingSettings(TaiBaseSettings):
    """Config for the document loaders behind :meth:`ResourceManager.load_file`."""

    model_config = SettingsConfigDict(env_prefix="FILE_LOADING_", frozen=True)

    # Hard cap (bytes) on the untrusted document bytes a loader decodes/parses
    # (HTML/EPUB via BeautifulSoup, PDF/XLSX/CSV/... via the path loaders). A
    # storage-id source bypasses ``fetch_url``'s own download cap, so this is the
    # bound before dispatch. Oversized -> loud raise, never a partial parse. Must
    # be positive.
    max_bytes: int = Field(default=25 * 1024 * 1024, gt=0)


class TemplateCacheSettings(TaiBaseSettings):
    """Config for the rendered-template cache: its TTL and max size."""

    model_config = SettingsConfigDict(
        env_prefix="TEMPLATE_CACHE_",
    )

    # Seconds a compiled template, and a template id storage answered "not found" for, is
    # kept; the only freshness bound for a template created or edited directly in the
    # storage backend, outside the platform's write paths (those evict at once).
    ttl: int | None = 60 * 5
    # How many compiled templates (and as many remembered-absent ids) each worker keeps; set
    # it at or above the number of stored templates the deployment renders.
    max_size: int | None = 1024

    @field_validator("ttl", "max_size", mode="before")
    @classmethod
    def parse_empty_or_none(cls, v: Any) -> Any:
        """Read an empty or ``none``/``null``/``undefined`` string as ``None`` (cache disabled)."""
        if v == "":
            return None

        if isinstance(v, str) and v.lower() in ("none", "null", "undefined"):
            return None

        return v


@settings_cache
def template_cache_settings() -> TemplateCacheSettings:
    """The cached :class:`TemplateCacheSettings` for this process."""
    return TemplateCacheSettings()


@settings_cache
def file_loading_settings() -> FileLoadingSettings:
    """The cached :class:`FileLoadingSettings` for this process."""
    return FileLoadingSettings()
