"""Storage management router: CRUD over a fake in-memory ``Storage`` provider,
plus the honest 501 when none is registered.

Handlers are driven directly (the router-test pattern). The provider is installed
by setting the process app's storage-registry provider to a stateful fake; the
absent case sets it to ``None`` so every management door answers a loud 501. The
presence read ``GET /api/storage`` lives in the core presence router and is covered
by ``test_storage_presence.py``.
"""

from __future__ import annotations

import base64
import json
import mimetypes
from types import SimpleNamespace
from typing import cast

import pytest
from starlette.requests import Request
from tai42_contract.storage import ObjectStat, Storage, assert_not_root

from tai42_skeleton.app import instance
from tai42_skeleton.operations.storage import _content_disposition
from tai42_skeleton.routers import storage as router
from tai42_skeleton.template import ResourceManager

from .._fakes.bus import FakeBus
from .._fakes.content_fixtures import (
    FILES_INSTALL_LINE,
    JAVASCRIPT,
    LOCKED_PDF_MESSAGE,
    NOTES_PDF,
    RAGGED_CSV,
    RAGGED_CSV_MESSAGE,
    SQL,
    SQL_LATIN1,
    locked_pdf,
    png,
)
from .._fakes.listing import keys_under
from .._fakes.recording_storage import RecordingStorage
from .._fakes.template_holders import HELD_DIRECTORY_REFUSAL, HELD_HOLDERS, HELD_TEMPLATE_REFUSAL, hold_jq_gate


class _FakeStorage(Storage):
    """An in-memory content store: ``id -> (bytes, content_type)``. Binary-native
    (overrides the byte/stat surface) so content type and downloads are exercised
    faithfully. ``delete``/``delete_dir`` raise ``FileNotFoundError`` for a missing
    target, pinning the router's 404 mapping."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str | None]] = {}

    async def load(self, path: str) -> str:
        return self._get(path)[0].decode("utf-8")

    async def list(self, directory: str | None = None) -> list[str]:
        return keys_under(list(self.objects), directory)

    async def upload(self, path: str, content: str) -> None:
        content_type, _ = mimetypes.guess_type(path)
        self.objects[path] = (content.encode("utf-8"), content_type)

    async def delete(self, path: str) -> None:
        self._get(path)
        del self.objects[path]

    async def delete_dir(self, path: str) -> None:
        assert_not_root(path)
        prefix = path.rstrip("/") + "/"
        matched = [key for key in self.objects if key == path or key.startswith(prefix)]
        if not matched:
            raise FileNotFoundError(path)
        for key in matched:
            del self.objects[key]

    async def load_bytes(self, path: str) -> bytes:
        return self._get(path)[0]

    async def upload_bytes(self, path: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[path] = (data, content_type)

    async def stat(self, path: str) -> ObjectStat:
        data, content_type = self._get(path)
        return ObjectStat(content_type=content_type, size=len(data))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        return self._get(path)[0][start : start + length]

    def _get(self, path: str) -> tuple[bytes, str | None]:
        if path not in self.objects:
            raise FileNotFoundError(path)
        return self.objects[path]


@pytest.fixture
def install(monkeypatch):
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


def _req(**path_params) -> Request:
    return cast(Request, SimpleNamespace(path_params=path_params))


def _body_req(body: bytes) -> Request:
    scope = {"type": "http", "method": "POST", "path": "/api/storage/resources", "headers": [], "query_string": b""}
    delivered = {"done": False}

    async def receive():
        if delivered["done"]:
            return {"type": "http.disconnect"}
        delivered["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _json(resp) -> dict:
    return json.loads(bytes(resp.body))


# -- 501 when no provider ----------------------------------------------------


async def test_list_501_when_absent(install):
    install(None)
    resp = await router.list_resources(_req())
    assert resp.status_code == 501
    assert _json(resp) == {"error": "storage needs a storage-provider plugin"}


async def test_stat_501_when_absent(install):
    install(None)
    resp = await router.stat_resource(_req(resource_id="x"))
    assert resp.status_code == 501


async def test_entry_501_when_absent(install):
    install(None)
    resp = await router.read_resource(_req(resource_id="x"))
    assert resp.status_code == 501
    assert _json(resp) == {"error": "storage needs a storage-provider plugin"}


async def test_download_501_when_absent(install):
    install(None)
    resp = await router.download_resource(_req(resource_id="x"))
    assert resp.status_code == 501


async def test_upload_501_when_absent(install):
    install(None)
    resp = await router.upload_resource(_body_req(b'{"id": "x", "content_text": "hi"}'))
    assert resp.status_code == 501


async def test_delete_501_when_absent(install):
    install(None)
    resp = await router.delete_resource(_req(resource_id="x"))
    assert resp.status_code == 501


async def test_delete_dir_501_when_absent(install):
    install(None)
    resp = await router.delete_dir(_req(dir_path="d"))
    assert resp.status_code == 501


# -- list / stat -------------------------------------------------------------


async def test_list_sorted(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"z.txt": (b"", "text/plain"), "a/b.txt": (b"", "text/plain")}
    resp = await router.list_resources(_req())
    assert _json(resp) == {"data": {"resources": ["a/b.txt", "z.txt"]}}


async def test_stat_returns_content_type(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"logo.png": (b"x", "image/png")}
    resp = await router.stat_resource(_req(resource_id="logo.png"))
    assert _json(resp) == {"data": {"id": "logo.png", "content_type": "image/png"}}


async def test_stat_content_type_null(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"data.unknownext": (b"x", None)}
    resp = await router.stat_resource(_req(resource_id="data.unknownext"))
    assert _json(resp) == {"data": {"id": "data.unknownext", "content_type": None}}


# -- download ----------------------------------------------------------------


async def test_download_content_type_and_disposition(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"docs/report.pdf": (b"%PDF-bytes", "application/pdf")}
    resp = await router.download_resource(_req(resource_id="docs/report.pdf"))
    assert resp.status_code == 200
    assert bytes(resp.body) == b"%PDF-bytes"
    assert resp.headers["content-type"] == "application/pdf"
    # An ASCII basename emits the quoted ``filename`` fallback plus the additive ``filename*``.
    assert resp.headers["content-disposition"] == "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"


async def test_download_octet_stream_fallback(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"blob": (b"\x00\x01", None)}
    resp = await router.download_resource(_req(resource_id="blob"))
    assert resp.headers["content-type"] == "application/octet-stream"


async def test_download_is_one_provider_read(install):
    store = cast(RecordingStorage, install(RecordingStorage({"docs/report.pdf": (b"%PDF-bytes", "application/pdf")})))
    resp = await router.download_resource(_req(resource_id="docs/report.pdf"))
    assert resp.headers["content-type"] == "application/pdf"
    assert store.calls == [("load_object", "docs/report.pdf")]


async def test_download_404_missing(install):
    install(_FakeStorage())
    resp = await router.download_resource(_req(resource_id="nope.txt"))
    assert resp.status_code == 404
    assert "nope.txt" in _json(resp)["error"]


# -- upload ------------------------------------------------------------------


async def test_upload_text(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    resp = await router.upload_resource(_body_req(b'{"id": "notes/todo.txt", "content_text": "buy milk"}'))
    assert resp.status_code == 200
    assert _json(resp) == {"data": {"id": "notes/todo.txt", "stored": True}}
    assert fake.objects["notes/todo.txt"][0] == b"buy milk"


async def test_upload_base64(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    encoded = base64.b64encode(b"\x00\x01\x02").decode("ascii")
    resp = await router.upload_resource(_body_req(json.dumps({"id": "b.bin", "content_base64": encoded}).encode()))
    assert resp.status_code == 200
    assert fake.objects["b.bin"][0] == b"\x00\x01\x02"


async def test_upload_overwrite(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"a.txt": (b"old", "text/plain")}
    await router.upload_resource(_body_req(b'{"id": "a.txt", "content_text": "new"}'))
    assert fake.objects["a.txt"][0] == b"new"


async def test_upload_requires_exactly_one_content_field(install):
    install(_FakeStorage())
    both = await router.upload_resource(_body_req(b'{"id": "a", "content_text": "t", "content_base64": "eA=="}'))
    assert both.status_code == 400
    both_error = _json(both)["error"]
    assert "content_text" in both_error
    assert "content_base64" in both_error
    neither = await router.upload_resource(_body_req(b'{"id": "a"}'))
    assert neither.status_code == 400


async def test_upload_invalid_base64_400(install):
    install(_FakeStorage())
    resp = await router.upload_resource(_body_req(b'{"id": "a", "content_base64": "not!base64!"}'))
    assert resp.status_code == 400
    assert "base64" in _json(resp)["error"]


async def test_upload_missing_id_400(install):
    install(_FakeStorage())
    resp = await router.upload_resource(_body_req(b'{"content_text": "t"}'))
    assert resp.status_code == 400
    assert "id" in _json(resp)["error"]


async def test_upload_invalid_json_400(install):
    install(_FakeStorage())
    resp = await router.upload_resource(_body_req(b"not json"))
    assert resp.status_code == 400
    assert "invalid JSON body" in _json(resp)["error"]


async def test_upload_non_object_body_400(install):
    install(_FakeStorage())
    resp = await router.upload_resource(_body_req(b'"a string"'))
    assert resp.status_code == 400
    assert "must be a JSON object" in _json(resp)["error"]


# -- delete ------------------------------------------------------------------


async def test_delete_resource(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"a.txt": (b"x", None)}
    resp = await router.delete_resource(_req(resource_id="a.txt"))
    assert _json(resp) == {"data": {"id": "a.txt", "deleted": True}}
    assert "a.txt" not in fake.objects


async def test_delete_404_missing(install):
    install(_FakeStorage())
    resp = await router.delete_resource(_req(resource_id="nope.txt"))
    assert resp.status_code == 404


async def test_delete_dir(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"d/a.txt": (b"x", None), "d/b.txt": (b"y", None), "other.txt": (b"z", None)}
    resp = await router.delete_dir(_req(dir_path="d"))
    assert _json(resp) == {"data": {"dir": "d", "deleted": True}}
    assert set(fake.objects) == {"other.txt"}


@pytest.mark.parametrize(
    ("handler", "params", "refusal"),
    [
        (router.delete_resource, {"resource_id": "jq/gate"}, HELD_TEMPLATE_REFUSAL),
        (router.delete_dir, {"dir_path": "jq"}, HELD_DIRECTORY_REFUSAL),
    ],
    ids=["file", "dir"],
)
@pytest.mark.usefixtures("admin_caller")
async def test_a_held_delete_is_409_naming_its_holders(install, monkeypatch, handler, params, refusal):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"jq/gate": (b".a", None)}
    hold_jq_gate(monkeypatch)
    resp = await handler(_req(**params))
    assert resp.status_code == 409
    assert _json(resp) == {"error": refusal, "holders": HELD_HOLDERS}
    assert list(fake.objects) == ["jq/gate"]


async def test_delete_dir_404_missing(install):
    install(_FakeStorage())
    resp = await router.delete_dir(_req(dir_path="nope"))
    assert resp.status_code == 404


# -- non-canonical keys: 400 with the guard's message on every id/path input -----
#
# The handlers receive the path parameter as given (a ``//``-prefixed request captures
# a leading ``/``), so every spelling reaches the guard here, the dot segments included.

_NON_CANONICAL = ["a//b", "./x", "x/.", "x/", "../x", "/a", "a/../b", ".", "a/.."]


def _non_canonical(key: str) -> str:
    return f"non-canonical store key {key!r}: a key is '/'-separated names with no empty, '.' or '..' segment"


@pytest.mark.parametrize("key", _NON_CANONICAL)
async def test_every_storage_door_refuses_a_non_canonical_key_and_changes_nothing(install, key: str):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"x": (b"kept", None), "a/b": (b"kept", None), "b": (b"kept", None)}
    before = dict(fake.objects)
    responses = [
        await router.stat_resource(_req(resource_id=key)),
        await router.download_resource(_req(resource_id=key)),
        await router.read_resource(_req(resource_id=key)),
        await router.delete_resource(_req(resource_id=key)),
        await router.delete_dir(_req(dir_path=key)),
        await router.upload_resource(_body_req(json.dumps({"id": key, "content_text": "t"}).encode())),
    ]
    for resp in responses:
        assert resp.status_code == 400
        assert _json(resp) == {"error": _non_canonical(key)}
    assert fake.objects == before


async def test_the_storage_and_template_upload_doors_refuse_a_key_in_the_same_words(install):
    from tai42_skeleton.routers import templates as templates_router

    install(_FakeStorage())
    storage = await router.upload_resource(_body_req(b'{"id": "a//b", "content_text": "t"}'))
    template = await templates_router.upload_template(_body_req(b'{"path": "a//b", "content": "t"}'))
    assert storage.status_code == template.status_code == 400
    assert _json(storage) == _json(template) == {"error": _non_canonical("a//b")}


async def test_list_leaves_out_an_unlisted_prefix(install, monkeypatch):
    from tai42_skeleton.conversations.media_meta import declare_media_prefix_marks

    from .._fakes.prefix_marks import fresh_prefix_marks

    fresh_prefix_marks(monkeypatch)
    declare_media_prefix_marks()
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {"inbound-media/x": (b"m", "image/png"), "notes/b.txt": (b"n", "text/plain")}
    resp = await router.list_resources(_req())
    assert _json(resp) == {"data": {"resources": ["notes/b.txt"]}}
    download = await router.download_resource(_req(resource_id="inbound-media/x"))
    assert download.status_code == 200


# -- provider boundary ValueError maps to 400 (never an uncaught 500) ----------


async def test_stat_provider_valueerror_is_400(install, monkeypatch):
    fake = cast(_FakeStorage, install(_FakeStorage()))

    async def boom(path: str):
        raise ValueError("path escapes the storage root")

    monkeypatch.setattr(fake, "stat", boom)
    resp = await router.stat_resource(_req(resource_id="ok.txt"))
    assert resp.status_code == 400
    assert "escapes" in _json(resp)["error"]


async def test_download_provider_valueerror_is_400(install, monkeypatch):
    fake = cast(_FakeStorage, install(_FakeStorage()))

    async def boom(path: str):
        raise ValueError("path escapes the storage root")

    monkeypatch.setattr(fake, "load_bytes", boom)
    resp = await router.download_resource(_req(resource_id="ok.txt"))
    assert resp.status_code == 400
    assert "escapes" in _json(resp)["error"]


async def test_delete_provider_valueerror_is_400(install, monkeypatch):
    fake = cast(_FakeStorage, install(_FakeStorage()))

    async def boom(path: str):
        raise ValueError("path escapes the storage root")

    monkeypatch.setattr(fake, "delete", boom)
    resp = await router.delete_resource(_req(resource_id="ok.txt"))
    assert resp.status_code == 400
    assert "escapes" in _json(resp)["error"]


async def test_upload_provider_valueerror_is_400(install, monkeypatch):
    fake = cast(_FakeStorage, install(_FakeStorage()))

    async def boom(path: str, data: bytes, content_type: str | None = None):
        raise ValueError("content is not storable by this provider")

    monkeypatch.setattr(fake, "upload_bytes", boom)
    encoded = base64.b64encode(b"\x00\x01\x02").decode("ascii")
    resp = await router.upload_resource(_body_req(json.dumps({"id": "b.bin", "content_base64": encoded}).encode()))
    assert resp.status_code == 400
    assert "not storable" in _json(resp)["error"]


# -- Content-Disposition filename is a well-formed quoted-string ---------------


async def test_download_disposition_escapes_quote_in_basename(install):
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = {'weird".txt': (b"x", "text/plain")}
    resp = await router.download_resource(_req(resource_id='weird".txt'))
    assert resp.status_code == 200
    # The literal ``"`` in the basename is backslash-escaped in the ASCII fallback, and the
    # additive ``filename*`` percent-encodes it — RFC 6266 § 4.3.
    assert resp.headers["content-disposition"] == 'attachment; filename="weird\\".txt"; filename*=UTF-8\'\'weird%22.txt'


def test_content_disposition_ascii_name_emits_both_parameters():
    # An ASCII name yields the quoted ``filename`` fallback plus an additive
    # ``filename*`` whose attr-char percent-encoding is a no-op.
    assert _content_disposition("report.pdf") == "attachment; filename=\"report.pdf\"; filename*=UTF-8''report.pdf"


def test_content_disposition_non_ascii_name_fallback_and_ext_value():
    # Every non-ASCII character becomes ``_`` in the fallback; ``filename*`` carries the
    # percent-encoded UTF-8 bytes.
    assert (
        _content_disposition("café☕.pdf")
        == "attachment; filename=\"caf__.pdf\"; filename*=UTF-8''caf%C3%A9%E2%98%95.pdf"
    )


def test_content_disposition_escaping_preserved():
    # A control character is stripped; ``"`` and ``\`` are backslash-escaped in the
    # fallback and percent-encoded in ``filename*``.
    assert (
        _content_disposition('a"b\\c\x01.txt')
        == 'attachment; filename="a\\"b\\\\c.txt"; filename*=UTF-8\'\'a%22b%5Cc.txt'
    )


# -- the entry door: GET /api/storage/entries/{id} ------------------------------------


def _with(install, objects: dict[str, tuple[bytes, str | None]]) -> _FakeStorage:
    fake = cast(_FakeStorage, install(_FakeStorage()))
    fake.objects = dict(objects)
    return fake


@pytest.mark.parametrize(("resource_id", "data"), [("queries/lookup.sql", SQL), ("app/greet.js", JAVASCRIPT)])
async def test_entry_text_types_answer_their_content(install, resource_id: str, data: bytes):
    _with(install, {resource_id: (data, None)})
    resp = await router.read_resource(_req(resource_id=resource_id))
    assert resp.status_code == 200
    assert _json(resp) == {
        "data": {"kind": "text", "id": resource_id, "content_type": None, "size": len(data), "content": data.decode()}
    }


async def test_entry_empty_object_is_empty_text(install):
    _with(install, {"empty.txt": (b"", "text/plain")})
    resp = await router.read_resource(_req(resource_id="empty.txt"))
    assert _json(resp)["data"] == {
        "kind": "text",
        "id": "empty.txt",
        "content_type": "text/plain",
        "size": 0,
        "content": "",
    }


async def test_entry_pdf_is_a_document(install):
    _with(install, {"docs/notes.pdf": (NOTES_PDF, "application/pdf")})
    data = _json(await router.read_resource(_req(resource_id="docs/notes.pdf")))["data"]
    assert data["kind"] == "document"
    assert "Quarterly notes" in data["content"]


async def test_entry_png_is_binary(install):
    image = png()
    _with(install, {"img/a.png": (image, "image/png")})
    resp = await router.read_resource(_req(resource_id="img/a.png"))
    assert _json(resp) == {
        "data": {"kind": "binary", "id": "img/a.png", "content_type": "image/png", "size": len(image)}
    }


async def test_entry_missing_is_404(install):
    _with(install, {})
    resp = await router.read_resource(_req(resource_id="nope.txt"))
    assert resp.status_code == 404
    assert _json(resp) == {"error": "resource 'nope.txt' not found"}


async def test_entry_refusals_answer_400_with_their_text(install):
    _with(
        install,
        {
            "queries/latin.sql": (SQL_LATIN1, None),
            "docs/locked.pdf": (locked_pdf(), None),
            "data/scores.csv": (RAGGED_CSV, None),
        },
    )
    latin = await router.read_resource(_req(resource_id="queries/latin.sql"))
    assert latin.status_code == 400
    assert _json(latin)["error"].startswith("resource 'queries/latin.sql' cannot be loaded: ")
    locked = await router.read_resource(_req(resource_id="docs/locked.pdf"))
    assert locked.status_code == 400
    assert _json(locked) == {"error": f"resource 'docs/locked.pdf' cannot be loaded: {LOCKED_PDF_MESSAGE}"}
    ragged = await router.read_resource(_req(resource_id="data/scores.csv"))
    assert ragged.status_code == 400
    assert _json(ragged) == {"error": f"resource 'data/scores.csv' cannot be loaded: {RAGGED_CSV_MESSAGE}"}


async def test_entry_genuine_provider_failure_is_not_mapped(install, monkeypatch):
    fake = _with(install, {"a.txt": (b"hello", None)})

    async def _fail(path: str, start: int, length: int) -> bytes:
        raise RuntimeError("upstream 502")

    monkeypatch.setattr(fake, "load_range", _fail)
    # Unmapped, it reaches the server's 500 handler.
    with pytest.raises(RuntimeError, match="upstream 502"):
        await router.read_resource(_req(resource_id="a.txt"))


@pytest.mark.usefixtures("files_extra_missing")
async def test_entry_without_the_files_extra_is_501_while_presence_still_answers(install, monkeypatch):
    from tai42_skeleton.routers import storage_presence

    _with(install, {"a.txt": (b"hello", None)})
    resp = await router.read_resource(_req(resource_id="a.txt"))
    assert resp.status_code == 501
    assert _json(resp) == {"error": FILES_INSTALL_LINE, "code": "files-not-configured"}
    monkeypatch.setattr(instance.app, "effective_router_modules", lambda: ["tai42_skeleton.routers.storage"])
    presence = await storage_presence.storage_info(_req())
    assert presence.status_code == 200
