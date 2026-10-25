"""Resource feature: the load/render impl over a :class:`~tai42_contract.storage.Storage`.

:class:`ResourceManager` loads content by storage ``id``, ``url``, or a raw file —
as text OR media — and renders Jinja templates fetched through the active
``Storage`` provider; ``TemplateCacheSettings`` configures the compiled-template
cache. ``MediaBlock`` is the media type it produces.
"""

from tai42_skeleton.template.errors import ContentNotLoadableError, TemplateNotFoundError, TemplateNotTextError
from tai42_skeleton.template.media import MediaBlock
from tai42_skeleton.template.resource_manager import ResourceManager
from tai42_skeleton.template.settings import TemplateCacheSettings, template_cache_settings

__all__ = [
    "ContentNotLoadableError",
    "MediaBlock",
    "ResourceManager",
    "TemplateCacheSettings",
    "TemplateNotFoundError",
    "TemplateNotTextError",
    "template_cache_settings",
]
