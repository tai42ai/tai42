"""Shared rig for the ``langchain_deep_agent`` backend test modules: the
template-missing sentinel and the fake resource manager the ``fake_tm`` fixture
installs in each module.
"""

from __future__ import annotations


class _TemplateMissingError(Exception):
    """Raised by the fake provider for an absent template id.

    Mirrors the real provider, whose ``fetch_template`` returns ``str`` (``""``
    for an empty body) or raises for a missing key — it never returns ``None``.
    """


class _FakeResourceManager:
    """In-memory stand-in for ``tai42_app.storage.resource_manager``.

    Stores raw key -> content. ``fetch_template`` is the RAW (non-jinja) read the
    backend uses; like the real provider it returns the stored text (``""`` for
    an empty body) or raises :class:`_TemplateMissingError` for a missing key.
    ``list_resources`` returns flat keys like the real providers (no leading
    slash). ``phantom_keys`` are listed but absent from the store, modelling a key
    that vanishes between the listing and the fetch.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.phantom_keys: set[str] = set()

    async def upload_template(self, path: str, content: str) -> None:
        self.store[path] = content

    async def list_resources(self) -> list[str]:
        return list(self.store) + [key for key in self.phantom_keys if key not in self.store]

    async def fetch_template(self, template_id: str) -> str:
        try:
            return self.store[template_id]
        except KeyError as exc:
            raise _TemplateMissingError(f"Template '{template_id}' not found.") from exc
