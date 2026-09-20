"""Resource feature: the load/render impl over a :class:`~tai42_contract.storage.Storage`.

:class:`ResourceManager` loads content by storage ``id``, ``url``, or a raw file —
as text OR media — and renders Jinja templates fetched through the active
``Storage`` provider; ``TemplateCacheSettings`` configures the compiled-template
cache. ``MediaBlock`` / ``ContentPart`` are the media types it produces.
"""

from tai42_skeleton.template.media import ContentPart, MediaBlock
from tai42_skeleton.template.resource_manager import ResourceManager, TemplateNotFoundError
from tai42_skeleton.template.settings import TemplateCacheSettings, template_cache_settings

__all__ = [
    "ContentPart",
    "MediaBlock",
    "ResourceManager",
    "TemplateCacheSettings",
    "TemplateNotFoundError",
    "template_cache_settings",
]
