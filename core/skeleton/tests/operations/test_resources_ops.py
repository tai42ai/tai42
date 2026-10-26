"""Op-level oracles for ``get_resource_by_id``.

``get_resource_by_id`` backs both methods of ``/api/resources/get`` (the GET fetch
door and the POST render door) and carries the route's typed error surface: it maps
``FileNotFoundError`` / ``ContentNotLoadableError`` / ``UnsafeTemplatePathError`` to
``NotFoundError`` (404) and ``BadRequestError`` (400). A READ door: not destructive,
projects normally.
"""

from __future__ import annotations

import mimetypes
from types import SimpleNamespace
from typing import ClassVar

import pytest
from fastmcp.utilities.types import Image
from tai42_contract.manifest import ApiToolsConfig
from tai42_contract.storage import ObjectStat, Storage
from tai42_contract.template import TemplatedText

import tai42_skeleton.operations.resources as resources_ops
from tai42_skeleton.operations import (
    BadRequestError,
    NotFoundError,
    NotSupportedError,
    OperationRegistry,
    operation_metadata_of,
)
from tai42_skeleton.operations.projection import project_operations
from tai42_skeleton.operations.resources import get_resource_by_id
from tai42_skeleton.template.media import MediaBlock

from .._fakes.content_fixtures import (
    FILES_INSTALL_LINE,
    LOCKED_PDF_MESSAGE,
    RAGGED_CSV,
    RAGGED_CSV_MESSAGE,
    locked_pdf,
)
from .._fakes.listing import keys_under
from .._fakes.recording_storage import RecordingStorage


class _ResourceManager:
    def __init__(
        self,
        *,
        loaded: str | MediaBlock = "",
        rendered: str = "",
        raise_exc: Exception | None = None,
    ) -> None:
        self.load_calls: list[str] = []
        self.render_calls: list[tuple[str | None, dict | None]] = []
        self._loaded = loaded
        self._rendered = rendered
        self._raise = raise_exc

    async def load_file(self, source: str) -> str | MediaBlock:
        self.load_calls.append(source)
        if self._raise is not None:
            raise self._raise
        return self._loaded

    async def render_templated_text(self, text: TemplatedText, locale: str | None = None) -> str:
        self.render_calls.append((text.content, text.kwargs))
        return self._rendered


def _bind(monkeypatch: pytest.MonkeyPatch, manager: _ResourceManager) -> None:
    fake_app = SimpleNamespace(storage=SimpleNamespace(resource_manager=manager))
    monkeypatch.setattr(resources_ops, "tai42_app", fake_app)


async def test_returns_loaded_text_unrendered(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _ResourceManager(loaded="raw {{ body }}")
    _bind(monkeypatch, manager)

    result = await get_resource_by_id("doc.txt")

    assert result == "raw {{ body }}"
    assert manager.load_calls == ["doc.txt"]
    assert manager.render_calls == []  # no kwargs -> no render


async def test_returns_media_block(monkeypatch: pytest.MonkeyPatch) -> None:
    block = Image(data=b"\x89PNG\r\n", format="png")
    manager = _ResourceManager(loaded=block)
    _bind(monkeypatch, manager)

    assert await get_resource_by_id("logo.png") is block


async def test_renders_text_with_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _ResourceManager(loaded="Hello {{ name }}", rendered="Hello Ada")
    _bind(monkeypatch, manager)

    result = await get_resource_by_id("greeting.j2", {"name": "Ada"})

    assert result == "Hello Ada"
    assert manager.render_calls == [("Hello {{ name }}", {"name": "Ada"})]


async def test_render_of_media_is_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # The op raises the typed 400 for a render ``ValueError``.
    manager = _ResourceManager(loaded=Image(data=b"\x89PNG", format="png"))
    _bind(monkeypatch, manager)

    with pytest.raises(BadRequestError, match="Cannot render media"):
        await get_resource_by_id("logo.png", {"x": 1})


async def test_missing_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    # The op maps a ``FileNotFoundError`` to the route 404.
    manager = _ResourceManager(raise_exc=FileNotFoundError("greeting.j2"))
    _bind(monkeypatch, manager)

    with pytest.raises(NotFoundError, match="not found"):
        await get_resource_by_id("greeting.j2")


async def test_rejects_traversal_id(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``get_resource_by_id`` funnels through ``ResourceManager.load``'s empty-scheme
    # branch, so the read-side guard fires on a traversal id before any storage read;
    # the op maps the guard's ``UnsafeTemplatePathError`` to the route 400.
    from tai42_skeleton.storage import StorageRegistry
    from tai42_skeleton.template import ResourceManager

    from ..template.test_resource_manager import _InMemoryStorage

    registry = StorageRegistry()
    registry.register_storage(_InMemoryStorage)
    real_manager = ResourceManager(registry.provider, on_evicted=lambda _eviction: None)
    monkeypatch.setattr(
        resources_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=real_manager))
    )

    with pytest.raises(BadRequestError):
        await get_resource_by_id("../x")


async def test_broken_jinja_is_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from jinja2 import TemplateSyntaxError

    manager = _ResourceManager(loaded="{{ oops")
    _bind(monkeypatch, manager)

    async def _broken(text, locale=None) -> str:
        raise TemplateSyntaxError("unexpected end of template", 1)

    monkeypatch.setattr(manager, "render_templated_text", _broken)
    with pytest.raises(BadRequestError, match="template error"):
        await get_resource_by_id("t.j2", {})


class _BytesStorage(Storage):
    """A binary-native store: ``load_bytes`` serves the raw stored bytes."""

    items: ClassVar[dict[str, bytes]] = {
        # Latin-1 text: detected as text, but its bytes are not UTF-8.
        "notes.txt": "Caf\u00e9 cr\u00e8me, na\u00efve r\u00e9sum\u00e9 d\u00e9j\u00e0 vu.\nSecond line.\n".encode(
            "latin-1"
        ),
        "doc.rtf": "{\\rtf1\\ansi caf\u00e9}".encode("latin-1"),
        # Bytes no loader handles.
        "blob.bin": bytes(range(256)) * 4,
    }

    async def load_bytes(self, path: str) -> bytes:
        try:
            return self.items[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    async def load(self, path: str) -> str:
        return (await self.load_bytes(path)).decode("utf-8")

    async def list(self, directory: str | None = None) -> list[str]:
        return keys_under(sorted(self.items), directory)

    async def upload(self, path: str, content: str) -> None:
        raise NotImplementedError

    async def delete(self, path: str) -> None:
        raise NotImplementedError

    async def delete_dir(self, path: str) -> None:
        raise NotImplementedError

    async def stat(self, path: str) -> ObjectStat:
        content_type, _ = mimetypes.guess_type(path)
        return ObjectStat(content_type=content_type, size=len(await self.load_bytes(path)))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        return (await self.load_bytes(path))[start : start + length]


def _bind_real(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.storage import StorageRegistry
    from tai42_skeleton.template import ResourceManager

    registry = StorageRegistry()
    registry.register_storage(_BytesStorage)
    manager = ResourceManager(registry.provider, on_evicted=lambda _eviction: None)
    monkeypatch.setattr(resources_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=manager)))


@pytest.mark.parametrize("resource_id", ["notes.txt", "doc.rtf"])
async def test_text_that_is_not_utf8_is_bad_request(monkeypatch: pytest.MonkeyPatch, resource_id: str) -> None:
    _bind_real(monkeypatch)
    with pytest.raises(BadRequestError, match=rf"{resource_id!r}.*not UTF-8"):
        await get_resource_by_id(resource_id)


async def test_unsupported_type_is_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_real(monkeypatch)
    with pytest.raises(BadRequestError, match="Unsupported file type"):
        await get_resource_by_id("blob.bin")


async def test_over_cap_is_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.template import file_loading

    _bind_real(monkeypatch)
    monkeypatch.setattr(file_loading, "file_loading_settings", lambda: SimpleNamespace(max_bytes=8))
    with pytest.raises(BadRequestError, match="FILE_LOADING_MAX_BYTES"):
        await get_resource_by_id("notes.txt")


def _bind_recording(monkeypatch: pytest.MonkeyPatch, objects: dict[str, bytes]) -> RecordingStorage:
    from tai42_skeleton.template import ResourceManager

    provider = RecordingStorage({key: (data, None) for key, data in objects.items()})
    manager = ResourceManager(provider, on_evicted=lambda _eviction: None)
    monkeypatch.setattr(resources_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=manager)))
    return provider


async def test_an_extractors_failure_is_bad_request_with_its_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind_recording(monkeypatch, {"docs/locked.pdf": locked_pdf(), "data/scores.csv": RAGGED_CSV})
    with pytest.raises(BadRequestError) as locked:
        await get_resource_by_id("docs/locked.pdf")
    assert locked.value.message == f"resource 'docs/locked.pdf' cannot be loaded: {LOCKED_PDF_MESSAGE}"
    with pytest.raises(BadRequestError) as ragged:
        await get_resource_by_id("data/scores.csv")
    assert ragged.value.message == f"resource 'data/scores.csv' cannot be loaded: {RAGGED_CSV_MESSAGE}"


@pytest.mark.usefixtures("files_extra_missing")
async def test_without_the_files_extra_is_not_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _bind_recording(monkeypatch, {"a.txt": b"hello"})
    with pytest.raises(NotSupportedError) as caught:
        await get_resource_by_id("a.txt")
    assert caught.value.message == FILES_INSTALL_LINE
    assert caught.value.extra == {"code": "files-not-configured"}
    assert provider.calls == []


def test_projects_as_a_read_tool() -> None:
    reg = OperationRegistry()
    reg.register(operation_metadata_of(get_resource_by_id))

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, dict] = {}

        def tool(self, *, force, name, tags, annotations):
            self.registered[name] = {"annotations": annotations}
            return lambda fn: fn

    app = SimpleNamespace(tools=_Rec())
    names = project_operations(app, ApiToolsConfig(), registry=reg)

    assert names == ["get_resource_by_id"]  # default-in read door
    assert app.tools.registered["get_resource_by_id"]["annotations"] is None  # not destructive
