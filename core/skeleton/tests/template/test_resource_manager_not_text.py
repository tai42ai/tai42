"""A stored object that is not UTF-8 text is refused as a template with a typed error.

Templates and other stored objects share one storage provider, so a template id can name
an image or any other binary object. Every by-id read of template text — fetch, schema,
undeclared variables, render, and an ``{% include %}`` dependency — raises
:class:`TemplateNotTextError` (a ``jinja2.TemplateError``) naming the id, never a raw
``UnicodeDecodeError``. Genuine storage failures keep their own types.
"""

from __future__ import annotations

import mimetypes

import pytest
from jinja2 import TemplateError
from tai42_contract.storage import ObjectStat, Storage
from tai42_contract.template import TemplatedText

import tai42_skeleton.operations.templates as templates_ops
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.storage import StorageRegistry
from tai42_skeleton.template import ResourceManager, TemplateNotTextError

from .._fakes.listing import keys_under

# A PNG signature: the 0x89 lead byte is not valid UTF-8.
_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


class _BytesStorage(Storage):
    """A binary-native provider whose ``load`` strictly decodes the stored bytes as UTF-8."""

    def __init__(self, items: dict[str, bytes]) -> None:
        self.items = dict(items)

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
        self.items[path] = content.encode("utf-8")

    async def delete(self, path: str) -> None:
        self.items.pop(path, None)

    async def delete_dir(self, path: str) -> None:
        raise NotImplementedError

    async def stat(self, path: str) -> ObjectStat:
        content_type, _ = mimetypes.guess_type(path)
        return ObjectStat(content_type=content_type, size=len(await self.load_bytes(path)))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        return (await self.load_bytes(path))[start : start + length]


class _BrokenStorage(_BytesStorage):
    def __init__(self) -> None:
        super().__init__({})

    async def load(self, path: str) -> str:
        raise ConnectionError("storage backend unreachable")


def _manager(storage: type[Storage]) -> ResourceManager:
    registry = StorageRegistry()
    registry.register_storage(storage)
    return ResourceManager(registry.provider, on_evicted=lambda _eviction: None)


class _Store(_BytesStorage):
    def __init__(self) -> None:
        super().__init__({"logo.png": _PNG, "page.j2": b"Head {% include 'logo.png' %}"})


def test_not_text_error_is_a_template_error() -> None:
    assert issubclass(TemplateNotTextError, TemplateError)


async def test_fetch_template_of_binary_object_is_not_text() -> None:
    with pytest.raises(TemplateNotTextError, match=r"'logo\.png' is not a text template"):
        await _manager(_Store).fetch_template("logo.png")


async def test_schema_of_binary_object_is_not_text() -> None:
    with pytest.raises(TemplateNotTextError, match=r"'logo\.png'"):
        await _manager(_Store).get_template_schema(template_id="logo.png")


async def test_undeclared_variables_of_binary_object_is_not_text() -> None:
    with pytest.raises(TemplateNotTextError, match=r"'logo\.png'"):
        await _manager(_Store).find_undeclared_variables(template_id="logo.png")


async def test_render_of_binary_object_is_not_text() -> None:
    with pytest.raises(TemplateNotTextError, match=r"'logo\.png'"):
        await _manager(_Store).render_templated_text(TemplatedText(id="logo.png"))


async def test_include_of_binary_object_is_not_text() -> None:
    with pytest.raises(TemplateNotTextError, match=r"'logo\.png'"):
        await _manager(_Store).render_templated_text(TemplatedText(id="page.j2"))


async def test_not_text_is_raised_again_on_every_read() -> None:
    """The refusal is never remembered as an absent template: each read reaches storage again."""
    manager = _manager(_Store)
    for _ in range(2):
        with pytest.raises(TemplateNotTextError):
            await manager.render_templated_text(TemplatedText(id="logo.png"))


async def test_storage_failure_keeps_its_own_type() -> None:
    with pytest.raises(ConnectionError, match="unreachable"):
        await _manager(_BrokenStorage).fetch_template("logo.png")


def _install(monkeypatch: pytest.MonkeyPatch, manager: ResourceManager) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(templates_ops, "tai42_app", SimpleNamespace(storage=SimpleNamespace(resource_manager=manager)))


async def test_render_template_op_of_binary_object_is_bad_request(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _manager(_Store))
    with pytest.raises(BadRequestError, match=r"'logo\.png' is not a text template"):
        await templates_ops.render_template(TemplatedText(id="logo.png"))
