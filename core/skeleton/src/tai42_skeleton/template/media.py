"""The media type produced and consumed by the :class:`ResourceManager`.

``MediaBlock`` is the model-side media union returned by
:meth:`ResourceManager.load_file` and the media tools — fastmcp already knows how
to turn each member into MCP content (``ImageContent`` / ``AudioContent`` /
``EmbeddedResource``).
"""

from __future__ import annotations

from fastmcp.utilities.types import Audio, File, Image

# The fastmcp media union a loaded resource decodes into. fastmcp serializes each
# member to MCP content via ``.to_image_content()`` / ``.to_audio_content()`` /
# ``.to_resource_content()``.
MediaBlock = Image | Audio | File


__all__ = ["MediaBlock"]
