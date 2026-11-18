"""The picture sources Slack can draw in an ``image`` block.

Slack's servers fetch an ``image`` block's ``image_url`` themselves, so the only source they can
load is an absolute ``http(s)`` url with a host. A ``data:`` url, or a relative served-media
reference such as ``/api/interactions/media/<id>``, has nothing for them to fetch. Every door
that draws an ``image`` block (a message's display media, a form's picture blocks) checks its
source here and refuses with its own error type.
"""

from __future__ import annotations

from urllib.parse import urlsplit

__all__ = ["is_loadable_image_url"]


def is_loadable_image_url(url: str) -> bool:
    """Whether ``url`` is an absolute ``http(s)`` url with a host, the only source Slack loads."""
    try:
        split = urlsplit(url)
    except ValueError:
        return False
    return split.scheme in ("http", "https") and bool(split.hostname)
