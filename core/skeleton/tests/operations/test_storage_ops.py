"""Op-level oracles for the storage operations.

``list_resources`` reads the storage provider and returns ``{"resources": [...]}``
with the resource ids SORTED (a deterministic order). The mutating ops project with
``destructiveHint``; ``list_resources`` (a read) does not.
"""

from __future__ import annotations

import mimetypes
from types import SimpleNamespace

import pytest
from tai42_contract.manifest import ApiToolsConfig
from tai42_contract.storage import ObjectStat, Storage

from tai42_skeleton.app import instance
from tai42_skeleton.conversations.media_meta import declare_media_prefix_marks
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    OperationFailedError,
    OperationRegistry,
    operation_metadata_of,
)
from tai42_skeleton.operations.projection import project_operations
from tai42_skeleton.operations.storage import (
    delete_dir,
    delete_resource,
    list_resources,
    read_resource,
    stat_resource,
    storage_info,
    upload_resource,
)
from tai42_skeleton.template import ResourceManager

from .._fakes.bus import FakeBus
from .._fakes.content_fixtures import (
    FILES_INSTALL_LINE,
    LOCKED_PDF_MESSAGE,
    NOTES_PDF,
    RAGGED_CSV,
    RAGGED_CSV_MESSAGE,
    locked_pdf,
    png,
)
from .._fakes.prefix_marks import fresh_prefix_marks
from .._fakes.recording_storage import RecordingStorage
from .._fakes.template_holders import (
    HELD_DIRECTORY_REFUSAL,
    HELD_HOLDERS,
    HELD_TEMPLATE_REFUSAL,
    STORED_PRESET_FAULT,
    fresh_referees,
    hold_jq_gate,
    hook_referee,
    stored_preset_fault_referee,
    value_error_referee,
)


class _FakeStorage(Storage):
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = {}

    async def load(self, path: str) -> str:
        return self.objects[path][0].decode("utf-8")

    async def list(self) -> list[str]:
        return list(self.objects)

    async def upload(self, path: str, content: str) -> None:
        content_type, _ = mimetypes.guess_type(path)
        self.objects[path] = (content.encode("utf-8"), content_type)

    async def delete(self, path: str) -> None:
        if path not in self.objects:
            raise FileNotFoundError(path)
        del self.objects[path]

    async def delete_dir(self, path: str) -> None:
        raise FileNotFoundError(path)

    async def load_bytes(self, path: str) -> bytes:
        if path not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[path][0]

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[path] = (data, content_type)

    async def stat(self, path: str) -> ObjectStat:
        if path not in self.objects:
            raise FileNotFoundError(path)
        data, content_type = self.objects[path]
        return ObjectStat(content_type=content_type, size=len(data))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        return (await self.load_bytes(path))[start : start + length]


@pytest.fixture
def install(monkeypatch: pytest.MonkeyPatch):
    def _install(provider: Storage | None) -> Storage | None:
        monkeypatch.setattr(instance.app._storage_registry, "_provider", provider)
        # A write evicts the template render state of what it wrote, here and fleet-wide.
        monkeypatch.setattr(
            instance.app,
            "_resource_manager_cache",
            ResourceManager(provider, on_evicted=instance.app._fire_template_evicted),
        )
        monkeypatch.setattr(instance.app, "_bus", FakeBus())
        return provider

    return _install


# -- list_resources ------------------


async def test_list_resources_returns_sorted_resources_object(install) -> None:
    provider = install(_FakeStorage())
    provider.objects = {"b.j2": (b"", None), "a.j2": (b"", None)}
    # Returns ``{"resources": [...]}`` with the ids sorted.
    assert await list_resources() == {"resources": ["a.j2", "b.j2"]}


async def test_list_resources_501_without_provider(install) -> None:
    install(None)
    with pytest.raises(NotSupportedError):
        await list_resources()


# -- op characterization -------------------------------------------------------


_STORAGE_ROUTER = "tai42_skeleton.routers.storage"
_TEMPLATES_ROUTER = "tai42_skeleton.routers.templates"


async def test_storage_info_absent(install, monkeypatch: pytest.MonkeyPatch) -> None:
    install(None)
    monkeypatch.setattr(instance.app, "effective_router_modules", lambda: [_STORAGE_ROUTER, _TEMPLATES_ROUTER])
    assert await storage_info() == {
        "present": False,
        "provider": None,
        "module": None,
        "surfaces": ["storage", "templates"],
    }


@pytest.mark.parametrize(
    ("modules", "surfaces"),
    [
        ([_STORAGE_ROUTER], ["storage"]),
        ([_TEMPLATES_ROUTER], ["templates"]),
        (["tai42_skeleton.routers.tools", _TEMPLATES_ROUTER, _STORAGE_ROUTER], ["storage", "templates"]),
        (["tai42_skeleton.routers.tools"], []),
    ],
)
async def test_storage_info_surfaces_follow_the_effective_router_list(
    install, monkeypatch: pytest.MonkeyPatch, modules: list[str], surfaces: list[str]
) -> None:
    provider = install(_FakeStorage())
    monkeypatch.setattr(instance.app, "effective_router_modules", lambda: modules)
    assert await storage_info() == {
        "present": True,
        "provider": "_FakeStorage",
        "module": type(provider).__module__,
        "surfaces": surfaces,
    }


async def test_storage_info_before_start_raises(install, monkeypatch: pytest.MonkeyPatch) -> None:
    install(None)
    monkeypatch.setattr(instance.app, "effective_router_modules", lambda: None)
    with pytest.raises(RuntimeError, match="storage_info needs a started deployment"):
        await storage_info()


async def test_stat_resource_missing_is_not_found(install) -> None:
    install(_FakeStorage())
    with pytest.raises(NotFoundError) as caught:
        await stat_resource("x")
    assert caught.value.message == "resource 'x' not found"


async def test_upload_resource_requires_exactly_one_content(install) -> None:
    install(_FakeStorage())
    from tai42_skeleton.operations import BadRequestError

    with pytest.raises(BadRequestError, match="exactly one"):
        await upload_resource("a", content_text="t", content_base64="eA==")
    with pytest.raises(BadRequestError, match="exactly one"):
        await upload_resource("a")


_NON_CANONICAL = ["a//b", "./x", "x/.", "x/", "../x"]


def _non_canonical(key: str) -> str:
    return f"non-canonical store key {key!r}: a key is '/'-separated names with no empty, '.' or '..' segment"


@pytest.mark.parametrize("key", _NON_CANONICAL)
async def test_every_key_taking_storage_operation_refuses_a_non_canonical_key(install, key: str) -> None:
    provider = install(_FakeStorage())
    provider.objects = {"x": (b"kept", None), "a/b": (b"kept", None)}
    before = dict(provider.objects)
    for call in (
        lambda: upload_resource(key, content_text="t"),
        lambda: stat_resource(key),
        lambda: read_resource(key),
        lambda: delete_resource(key),
        lambda: delete_dir(key),
    ):
        with pytest.raises(BadRequestError) as caught:
            await call()
        assert caught.value.message == _non_canonical(key)
    assert provider.objects == before


async def test_upload_resource_refuses_a_backslash_key_as_unsafe(install) -> None:
    provider = install(_FakeStorage())
    with pytest.raises(BadRequestError) as caught:
        await upload_resource("a\\b", content_text="t")
    assert caught.value.message == "unsafe template path: 'a\\\\b'"
    assert provider.objects == {}


async def test_list_resources_leaves_out_an_unlisted_prefix_but_its_objects_stay_reachable(
    install, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh_prefix_marks(monkeypatch)
    declare_media_prefix_marks()
    provider = install(_FakeStorage())
    provider.objects = {"inbound-media/x": (b"m", "image/png"), "notes/b.txt": (b"n", "text/plain")}
    assert await list_resources() == {"resources": ["notes/b.txt"]}
    assert (await stat_resource("inbound-media/x"))["id"] == "inbound-media/x"
    provider.objects["inbound-media/x"] = (png(), "image/png")
    assert (await read_resource("inbound-media/x"))["kind"] == "binary"
    provider.objects["inbound-media/x"] = (b"m", "image/png")
    assert await delete_resource("inbound-media/x") == {"id": "inbound-media/x", "deleted": True}


async def test_a_prefix_marked_only_not_backed_up_stays_listed(install, monkeypatch: pytest.MonkeyPatch) -> None:
    marks = fresh_prefix_marks(monkeypatch)
    marks.not_backed_up.declare("volatile/")
    provider = install(_FakeStorage())
    provider.objects = {"volatile/x": (b"v", None), "notes/b.txt": (b"n", None)}
    assert await list_resources() == {"resources": ["notes/b.txt", "volatile/x"]}


async def test_upload_resource_rejects_non_string_content(install) -> None:
    # The HTTP extractor passes the raw body through; the op guards non-string
    # content that the MCP tool schema (str | None) would already reject.
    install(_FakeStorage())
    from tai42_skeleton.operations import BadRequestError

    with pytest.raises(BadRequestError, match="'content_text' must be a string"):
        await upload_resource("a", content_text=123)  # type: ignore[arg-type]
    with pytest.raises(BadRequestError, match="'content_base64' must be a base64 string"):
        await upload_resource("a", content_base64=123)  # type: ignore[arg-type]


async def test_delete_resource_missing_is_not_found(install) -> None:
    install(_FakeStorage())
    from tai42_skeleton.operations import NotFoundError

    with pytest.raises(NotFoundError):
        await delete_resource("nope.txt")


# -- a held object's delete is refused -----------------------------------------


@pytest.fixture
def held_store(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A real manager over ``jq/gate`` and ``jq/free``, recording its evictions, behind a fleet bus."""
    provider = RecordingStorage({"jq/gate": (b".a", None), "jq/free": (b".b", None)})
    evicted: list[object] = []
    bus = FakeBus(remotes=["serve-w1"])
    monkeypatch.setattr(instance.app._storage_registry, "_provider", provider)
    monkeypatch.setattr(instance.app, "_resource_manager_cache", ResourceManager(provider, on_evicted=evicted.append))
    monkeypatch.setattr(instance.app, "_bus", bus)
    return SimpleNamespace(provider=provider, evicted=evicted, bus=bus)


_DELETES = [
    pytest.param(lambda: delete_resource("jq/gate"), HELD_TEMPLATE_REFUSAL, "stored template 'jq/gate'", id="file"),
    pytest.param(lambda: delete_dir("jq"), HELD_DIRECTORY_REFUSAL, "directory 'jq'", id="dir"),
]


@pytest.mark.parametrize(("delete", "refusal", "head"), _DELETES)
@pytest.mark.usefixtures("admin_caller")
async def test_a_held_delete_is_refused_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace, delete, refusal: str, head: str
) -> None:
    hold_jq_gate(monkeypatch)
    with pytest.raises(ConflictError) as excinfo:
        await delete()
    assert str(excinfo.value) == refusal
    assert excinfo.value.extra == {"holders": HELD_HOLDERS}
    assert sorted(held_store.provider.objects) == ["jq/free", "jq/gate"]
    assert held_store.evicted == []
    assert held_store.bus.publish_calls == []


async def test_an_unheld_delete_succeeds(monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace) -> None:
    hold_jq_gate(monkeypatch)
    assert await delete_resource("jq/free") == {"id": "jq/free", "deleted": True}
    assert sorted(held_store.provider.objects) == ["jq/gate"]
    assert len(held_store.bus.publish_calls) == 1


async def test_an_unheld_directory_delete_succeeds(
    monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace
) -> None:
    fresh_referees(monkeypatch).register(hook_referee)
    held_store.provider.objects.pop("jq/gate")
    assert await delete_dir("jq") == {"dir": "jq", "deleted": True}
    assert held_store.provider.objects == {}


@pytest.mark.parametrize(("delete", "refusal", "head"), _DELETES)
async def test_a_referee_fault_fails_the_delete_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace, delete, refusal: str, head: str
) -> None:
    fresh_referees(monkeypatch).register(value_error_referee)
    with pytest.raises(OperationFailedError) as excinfo:
        await delete()
    assert str(excinfo.value) == f"{head} cannot be deleted: a holder could not be read (ValueError)"
    assert isinstance(excinfo.value.__cause__, ValueError)
    assert "'x'" not in str(excinfo.value)
    assert sorted(held_store.provider.objects) == ["jq/free", "jq/gate"]
    assert held_store.bus.publish_calls == []


@pytest.mark.parametrize(("delete", "refusal", "head"), _DELETES)
async def test_a_stored_preset_fault_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, held_store: SimpleNamespace, delete, refusal: str, head: str
) -> None:
    fresh_referees(monkeypatch).register(stored_preset_fault_referee)
    with pytest.raises(OperationFailedError) as excinfo:
        await delete()
    assert excinfo.value is STORED_PRESET_FAULT
    assert sorted(held_store.provider.objects) == ["jq/free", "jq/gate"]
    assert held_store.bus.publish_calls == []


# -- projection: the mutating ops carry destructiveHint ------------------------


def test_projection_destructive_hints() -> None:
    # ``upload_resource`` declares ``destructive`` on the decorator; the DELETE ops
    # are destructive via the adapter's DELETE auto-force at route registration (their
    # ``x-destructive`` is pinned by the OpenAPI spec, not asserted here in isolation).
    reg = OperationRegistry()
    for op in (upload_resource, list_resources, storage_info):
        reg.register(operation_metadata_of(op))

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, dict] = {}

        def tool(self, *, force, name, tags, annotations):
            self.registered[name] = {"annotations": annotations}
            return lambda fn: fn

    app = SimpleNamespace(tools=_Rec())
    names = project_operations(app, ApiToolsConfig(expose_destructive=True), registry=reg)

    assert {"upload_resource", "list_resources", "storage_info"} == set(names)
    assert app.tools.registered["upload_resource"]["annotations"].destructiveHint is True
    assert app.tools.registered["list_resources"]["annotations"] is None  # read
    assert app.tools.registered["storage_info"]["annotations"] is None  # read


async def test_upload_resource_path_conflict_maps_to_409(install) -> None:
    from tai42_contract.storage import StoragePathConflictError

    from tai42_skeleton.operations import ConflictError

    class _ConflictingStorage(_FakeStorage):
        async def upload(self, path: str, content: str) -> None:
            raise StoragePathConflictError(path, ["a/b/c.j2"])

    install(_ConflictingStorage())
    with pytest.raises(ConflictError) as exc:
        await upload_resource("a/b", content_text="hi")
    assert exc.value.status == 409
    assert "a/b/c.j2" in exc.value.message


# -- read_resource ----------------------------------------------------------------


async def test_read_resource_answers_a_text_entry(install) -> None:
    provider = install(_FakeStorage())
    provider.objects = {"notes/todo.txt": (b"buy milk\n", "text/plain")}
    assert await read_resource("notes/todo.txt") == {
        "kind": "text",
        "id": "notes/todo.txt",
        "content_type": "text/plain",
        "size": 9,
        "content": "buy milk\n",
    }


async def test_read_resource_answers_a_document_entry_with_the_extracted_text(install) -> None:
    provider = install(_FakeStorage())
    provider.objects = {"docs/notes.pdf": (NOTES_PDF, "application/pdf")}
    entry = await read_resource("docs/notes.pdf")
    assert entry["kind"] == "document"
    assert "Quarterly notes" in entry["content"]
    assert "schema" not in entry


async def test_read_resource_answers_a_binary_entry_with_no_schema(install) -> None:
    provider = install(_FakeStorage())
    image = png()
    provider.objects = {"img/a.png": (image, "image/png")}
    assert await read_resource("img/a.png") == {
        "kind": "binary",
        "id": "img/a.png",
        "content_type": "image/png",
        "size": len(image),
    }


async def test_read_resource_missing_is_not_found(install) -> None:
    install(_FakeStorage())
    with pytest.raises(NotFoundError, match=r"^resource 'nope\.txt' not found$"):
        await read_resource("nope.txt")


async def test_read_resource_without_a_provider_is_the_501(install) -> None:
    install(None)
    with pytest.raises(NotSupportedError):
        await read_resource("a.txt")


async def test_read_resource_refuses_an_extractors_failure_with_its_text(install) -> None:
    provider = install(_FakeStorage())
    provider.objects = {"docs/locked.pdf": (locked_pdf(), None), "data/scores.csv": (RAGGED_CSV, None)}
    with pytest.raises(BadRequestError) as locked:
        await read_resource("docs/locked.pdf")
    assert locked.value.message == f"resource 'docs/locked.pdf' cannot be loaded: {LOCKED_PDF_MESSAGE}"
    with pytest.raises(BadRequestError) as ragged:
        await read_resource("data/scores.csv")
    assert ragged.value.message == f"resource 'data/scores.csv' cannot be loaded: {RAGGED_CSV_MESSAGE}"


@pytest.mark.usefixtures("files_extra_missing")
async def test_read_resource_without_the_files_extra_is_the_501(install) -> None:
    provider = install(_FakeStorage())
    provider.objects = {"a.txt": (b"hello", None)}
    with pytest.raises(NotSupportedError) as caught:
        await read_resource("a.txt")
    assert caught.value.message == FILES_INSTALL_LINE
    assert caught.value.extra == {"code": "files-not-configured"}


@pytest.mark.usefixtures("files_extra_missing")
async def test_the_projected_read_resource_tool_raises_the_install_line(install) -> None:
    from fastmcp.exceptions import ToolError

    from tai42_skeleton.operations.projection import _make_tool

    provider = install(_FakeStorage())
    provider.objects = {"a.txt": (b"hello", None)}
    tool = _make_tool(operation_metadata_of(read_resource))
    with pytest.raises(ToolError) as caught:
        await tool(resource_id="a.txt")
    assert str(caught.value) == FILES_INSTALL_LINE
