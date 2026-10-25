"""The ``file_loader`` builtin: a thin pass-through to
``ResourceManager.load_file`` that accepts a url OR a resource id and returns text
OR a media block, forwarding the source verbatim and never swallowing an error.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastmcp.utilities.types import Image

from tai42_skeleton.operations import NotSupportedError
from tai42_skeleton.template import ContentNotLoadableError
from tai42_skeleton.template.media import MediaBlock
from tai42_skeleton.tools.builtin import file_loader as builtin_file_loader

from ..._fakes.content_fixtures import FILES_INSTALL_LINE, LOCKED_PDF_MESSAGE, locked_pdf
from ..._fakes.recording_storage import RecordingStorage


class _ResourceManager:
    def __init__(self, *, loaded: str | MediaBlock = "", raise_exc: Exception | None = None) -> None:
        self.load_calls: list[str] = []
        self._loaded = loaded
        self._raise = raise_exc

    async def load_file(self, source: str) -> str | MediaBlock:
        self.load_calls.append(source)
        if self._raise is not None:
            raise self._raise
        return self._loaded


class _Storage:
    def __init__(self, resource_manager: _ResourceManager) -> None:
        self.resource_manager = resource_manager


class _FakeApp:
    def __init__(self, resource_manager: _ResourceManager) -> None:
        self.storage = _Storage(resource_manager)


async def test_file_loader_returns_text_for_url(bind_app) -> None:
    manager = _ResourceManager(loaded="downloaded file body")
    bind_app(_FakeApp(manager))

    result = await builtin_file_loader.file_loader("https://example.com/doc.txt")

    assert result == "downloaded file body"
    assert manager.load_calls == ["https://example.com/doc.txt"]


async def test_file_loader_returns_media_for_resource_id(bind_app) -> None:
    block = Image(data=b"\x89PNG\r\n", format="png")
    manager = _ResourceManager(loaded=block)
    bind_app(_FakeApp(manager))

    result = await builtin_file_loader.file_loader("images/logo.png")

    assert result is block
    assert manager.load_calls == ["images/logo.png"]


async def test_file_loader_propagates_error(bind_app) -> None:
    manager = _ResourceManager(raise_exc=ValueError("Unsupported file type"))
    bind_app(_FakeApp(manager))

    with pytest.raises(ValueError, match="Unsupported file type"):
        await builtin_file_loader.file_loader("blob.bin")


async def test_file_loader_rejects_traversal_id(bind_app) -> None:
    # file_loader funnels through ResourceManager.load's empty-scheme branch, so the
    # read-side guard fires on a traversal storage id before any storage read.
    from tai42_skeleton.storage import StorageRegistry
    from tai42_skeleton.template import ResourceManager
    from tai42_skeleton.template.path_guard import UnsafeTemplatePathError

    from ...template.test_resource_manager import _InMemoryStorage

    registry = StorageRegistry()
    registry.register_storage(_InMemoryStorage)
    real_manager = ResourceManager(registry.provider, on_evicted=lambda _eviction: None)
    bind_app(SimpleNamespace(storage=SimpleNamespace(resource_manager=real_manager)))

    with pytest.raises(UnsafeTemplatePathError):
        await builtin_file_loader.file_loader("../x")


def _bind_recording(bind_app, objects: dict[str, bytes]) -> RecordingStorage:
    from tai42_skeleton.template import ResourceManager

    provider = RecordingStorage({key: (data, None) for key, data in objects.items()})
    manager = ResourceManager(provider, on_evicted=lambda _eviction: None)
    bind_app(SimpleNamespace(storage=SimpleNamespace(resource_manager=manager)))
    return provider


async def test_file_loader_raises_an_extractors_failure_with_its_text(bind_app) -> None:
    _bind_recording(bind_app, {"docs/locked.pdf": locked_pdf()})
    with pytest.raises(ContentNotLoadableError) as caught:
        await builtin_file_loader.file_loader("docs/locked.pdf")
    assert str(caught.value) == LOCKED_PDF_MESSAGE


@pytest.mark.usefixtures("files_extra_missing")
async def test_file_loader_without_the_files_extra_is_the_501(bind_app) -> None:
    provider = _bind_recording(bind_app, {"a.txt": b"hello"})
    with pytest.raises(NotSupportedError) as caught:
        await builtin_file_loader.file_loader("a.txt")
    assert caught.value.message == FILES_INSTALL_LINE
    assert caught.value.status == 501
    assert provider.calls == []
