"""The stored-entry reader: one stored object as a text, document or binary entry, read within bounds."""

from __future__ import annotations

import ast
import inspect
from types import SimpleNamespace

import pytest
from tai42_contract.app import tai42_app

from tai42_skeleton.app import instance
from tai42_skeleton.conversations.media_meta import INBOUND_MEDIA_BLOB_PREFIX, declare_media_prefix_marks
from tai42_skeleton.operations import BadRequestError, NotFoundError, NotSupportedError
from tai42_skeleton.storage import entries
from tai42_skeleton.storage.entries import read_stored_entry
from tai42_skeleton.template import file_loading

from .._fakes.content_fixtures import (
    FILES_INSTALL_LINE,
    HTML,
    JAVASCRIPT,
    LOCKED_PDF_MESSAGE,
    NOTES_PDF,
    RAGGED_CSV,
    RAGGED_CSV_MESSAGE,
    SQL,
    SQL_LATIN1,
    locked_pdf,
    png,
    zip_archive,
)
from .._fakes.prefix_marks import fresh_prefix_marks
from .._fakes.recording_storage import RecordingStorage


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> RecordingStorage:
    provider = RecordingStorage()
    monkeypatch.setattr(instance.app._storage_registry, "_provider", provider)
    return provider


def _reads(store: RecordingStorage) -> list[tuple]:
    return [call for call in store.calls if call[0] in ("stat", "load_range", "load_bytes")]


def _not_loadable(resource_id: str, reason: str) -> str:
    return f"resource {resource_id!r} cannot be loaded: {reason}"


# -- the kinds -------------------------------------------------------------------


async def test_utf8_text_is_a_text_entry_with_its_content_and_size(store: RecordingStorage) -> None:
    store.put("notes/todo.txt", b"buy milk\n", "text/plain")
    assert await read_stored_entry("notes/todo.txt") == {
        "kind": "text",
        "id": "notes/todo.txt",
        "content_type": "text/plain",
        "size": 9,
        "content": "buy milk\n",
    }


async def test_short_utf8_text_the_detector_cannot_name_is_a_text_entry(store: RecordingStorage) -> None:
    store.put("parts/greeting", "situação\n".encode())
    assert await read_stored_entry("parts/greeting") == {
        "kind": "text",
        "id": "parts/greeting",
        "content_type": None,
        "size": 11,
        "content": "situação\n",
    }


async def test_png_bytes_are_a_binary_entry_with_no_content(store: RecordingStorage) -> None:
    image = png()
    store.put("img/a.png", image, "image/png")
    assert await read_stored_entry("img/a.png") == {
        "kind": "binary",
        "id": "img/a.png",
        "content_type": "image/png",
        "size": len(image),
    }
    assert ("load_bytes", "img/a.png") not in store.calls


async def test_html_is_a_text_entry_with_the_classifiers_text(store: RecordingStorage) -> None:
    store.put("pages/a.html", HTML, "text/html")
    entry = await read_stored_entry("pages/a.html")
    assert entry["kind"] == "text"
    assert entry["content"] == file_loading.load_content(HTML, "pages/a.html")


async def test_a_zip_is_a_binary_entry(store: RecordingStorage) -> None:
    archive = zip_archive()
    store.put("dl/a.zip", archive, "application/zip")
    assert (await read_stored_entry("dl/a.zip"))["kind"] == "binary"


@pytest.mark.parametrize(("resource_id", "data"), [("queries/lookup.sql", SQL), ("app/greet.js", JAVASCRIPT)])
async def test_a_text_type_with_no_handler_is_a_text_entry_with_its_exact_content(
    store: RecordingStorage, resource_id: str, data: bytes
) -> None:
    store.put(resource_id, data)
    entry = await read_stored_entry(resource_id)
    assert entry["kind"] == "text"
    assert entry["content"] == data.decode()


async def test_the_empty_object_is_empty_text_read_by_its_stat_alone(store: RecordingStorage) -> None:
    store.put("empty.txt", b"", "text/plain")
    assert await read_stored_entry("empty.txt") == {
        "kind": "text",
        "id": "empty.txt",
        "content_type": "text/plain",
        "size": 0,
        "content": "",
    }
    assert _reads(store) == [("stat", "empty.txt")]


async def test_a_pdf_is_a_document_entry_carrying_its_extracted_text(store: RecordingStorage) -> None:
    store.put("docs/notes.pdf", NOTES_PDF, "application/pdf")
    entry = await read_stored_entry("docs/notes.pdf")
    assert entry["kind"] == "document"
    assert entry["size"] == len(NOTES_PDF)
    assert entry["content_type"] == "application/pdf"
    assert "Quarterly notes" in entry["content"]


# -- the refusals ----------------------------------------------------------------


async def test_latin1_text_is_refused_with_the_strict_decode_error(store: RecordingStorage) -> None:
    store.put("queries/latin.sql", SQL_LATIN1)
    with pytest.raises(BadRequestError) as caught:
        await read_stored_entry("queries/latin.sql")
    assert caught.value.message.startswith("resource 'queries/latin.sql' cannot be loaded: ")
    assert "is detected as text but is not UTF-8" in caught.value.message


async def test_text_over_the_cap_is_refused_without_reading_it_whole(
    store: RecordingStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_loading, "file_loading_settings", lambda: SimpleNamespace(max_bytes=8))
    store.put("notes/long.txt", b"more than eight bytes\n")
    with pytest.raises(BadRequestError) as caught:
        await read_stored_entry("notes/long.txt")
    assert caught.value.message == _not_loadable(
        "notes/long.txt", "document is 22 bytes, over the 8-byte FILE_LOADING_MAX_BYTES cap"
    )
    assert ("load_bytes", "notes/long.txt") not in store.calls


async def test_an_extractors_failure_is_refused_with_its_exact_text(store: RecordingStorage) -> None:
    store.put("docs/locked.pdf", locked_pdf())
    store.put("data/scores.csv", RAGGED_CSV)
    with pytest.raises(BadRequestError) as locked:
        await read_stored_entry("docs/locked.pdf")
    assert locked.value.message == _not_loadable("docs/locked.pdf", LOCKED_PDF_MESSAGE)
    with pytest.raises(BadRequestError) as ragged:
        await read_stored_entry("data/scores.csv")
    assert ragged.value.message == _not_loadable("data/scores.csv", RAGGED_CSV_MESSAGE)


async def test_a_missing_object_is_not_found(store: RecordingStorage) -> None:
    with pytest.raises(NotFoundError, match=r"^resource 'nope\.txt' not found$"):
        await read_stored_entry("nope.txt")


@pytest.mark.parametrize("key", ["a//b", "../x", "./x", "x/"])
async def test_a_non_canonical_key_is_refused_before_any_read(store: RecordingStorage, key: str) -> None:
    with pytest.raises(BadRequestError, match="non-canonical store key"):
        await read_stored_entry(key)
    assert store.calls == []


async def test_no_provider_is_the_501(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(instance.app._storage_registry, "_provider", None)
    with pytest.raises(NotSupportedError, match="storage needs a storage-provider plugin"):
        await read_stored_entry("a.txt")


async def test_a_provider_boundary_value_error_is_a_400(store: RecordingStorage, monkeypatch) -> None:
    async def _refuse(path: str):
        raise ValueError(f"path escapes the root: {path}")

    monkeypatch.setattr(store, "stat", _refuse)
    with pytest.raises(BadRequestError, match=r"^path escapes the root: a\.txt$"):
        await read_stored_entry("a.txt")


@pytest.mark.parametrize("data", [b"hello", b"hello\n" * 2000], ids=["sample-read", "whole-read"])
async def test_a_genuine_provider_failure_propagates(store: RecordingStorage, monkeypatch, data: bytes) -> None:
    store.put("a.txt", data)
    reads = 0
    real_load_range = store.load_range

    async def _fail_last_read(path: str, start: int, length: int) -> bytes:
        nonlocal reads
        reads += 1
        # The small object fails on its one sample read; the large one passes its sample
        # read and fails on the whole read that follows it.
        if reads == 1 and len(data) > length:
            return await real_load_range(path, start, length)
        raise RuntimeError("upstream 502")

    monkeypatch.setattr(store, "load_range", _fail_last_read)
    with pytest.raises(RuntimeError, match="upstream 502"):
        await read_stored_entry("a.txt")


# -- the bounded read --------------------------------------------------------------


async def test_a_large_binary_object_is_classified_from_two_windows_and_never_read_whole(
    store: RecordingStorage,
) -> None:
    size = 26214400
    store.put("media/big.png", png()[:64] + b"\x00" * (size - 64), "image/png")
    assert await read_stored_entry("media/big.png") == {
        "kind": "binary",
        "id": "media/big.png",
        "content_type": "image/png",
        "size": size,
    }
    assert _reads(store) == [
        ("stat", "media/big.png"),
        ("load_range", "media/big.png", 0, 4096),
        ("load_range", "media/big.png", size - 4096, 4096),
    ]


async def test_an_object_within_two_windows_is_sampled_with_one_ranged_read(store: RecordingStorage) -> None:
    image = png()[:64] + b"\x00" * (6000 - 64)
    store.put("media/small.png", image, "image/png")
    await read_stored_entry("media/small.png")
    assert _reads(store) == [("stat", "media/small.png"), ("load_range", "media/small.png", 0, 6000)]


@pytest.fixture
def detections(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    """Every byte string the content classifier inspects, in order."""
    seen: list[bytes] = []
    identify = file_loading._identify

    def _counting(data: bytes, source: str | None):
        seen.append(data)
        return identify(data, source)

    monkeypatch.setattr(file_loading, "_identify", _counting)
    return seen


async def test_a_text_object_within_two_windows_is_read_once_and_classified_once(
    store: RecordingStorage, detections: list[bytes]
) -> None:
    store.put("notes/todo.txt", b"buy milk\n", "text/plain")
    assert (await read_stored_entry("notes/todo.txt"))["content"] == "buy milk\n"
    assert _reads(store) == [("stat", "notes/todo.txt"), ("load_range", "notes/todo.txt", 0, 9)]
    assert detections == [b"buy milk\n"]


async def test_a_text_object_past_two_windows_is_sampled_read_whole_once_and_classified_once(
    store: RecordingStorage, detections: list[bytes]
) -> None:
    data = b"line of plain text for the reader\n" * 400
    size = len(data)
    store.put("notes/long.txt", data, "text/plain")
    assert (await read_stored_entry("notes/long.txt"))["content"] == data.decode()
    assert _reads(store) == [
        ("stat", "notes/long.txt"),
        ("load_range", "notes/long.txt", 0, 4096),
        ("load_range", "notes/long.txt", size - 4096, 4096),
        ("load_range", "notes/long.txt", 0, 25 * 1024 * 1024 + 1),
    ]
    assert detections == [data[:4096] + data[-4096:]]


async def test_an_object_grown_past_the_cap_after_its_stat_is_refused_on_the_bytes_read(
    store: RecordingStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(file_loading, "file_loading_settings", lambda: SimpleNamespace(max_bytes=10_000))
    stated = b"line of plain text for the reader\n" * 270
    store.put("notes/grown.txt", stated * 20, "text/plain")
    real_stat = store.stat

    async def _stale_stat(path: str):
        # The stat reports the object's size before it grew to twenty times that.
        return (await real_stat(path)).model_copy(update={"size": len(stated)})

    monkeypatch.setattr(store, "stat", _stale_stat)
    with pytest.raises(BadRequestError) as caught:
        await read_stored_entry("notes/grown.txt")
    assert caught.value.message == _not_loadable(
        "notes/grown.txt", "document is over the 10000-byte FILE_LOADING_MAX_BYTES cap"
    )
    assert _reads(store)[-1] == ("load_range", "notes/grown.txt", 0, 10_001)
    assert ("load_bytes", "notes/grown.txt") not in store.calls


def test_the_reader_decides_through_the_shared_classifier_alone() -> None:
    source = inspect.getsource(entries)
    # The reader runs the classifier off the event loop: ``asyncio.to_thread(classify, ...)``.
    thread_calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "to_thread"
    ]
    called = {arg.id for call in thread_calls for arg in call.args[:1] if isinstance(arg, ast.Name)}
    assert {"classify", "load_classified"} <= called
    assert ".decode(" not in source


# -- unlisted keys stay readable -------------------------------------------------------


async def test_an_unlisted_key_is_still_read_by_id(store: RecordingStorage, monkeypatch) -> None:
    fresh_prefix_marks(monkeypatch)
    with tai42_app.bound(instance.app):
        declare_media_prefix_marks()
    assert instance.app.storage.unlisted_prefixes() == {INBOUND_MEDIA_BLOB_PREFIX}
    store.put(f"{INBOUND_MEDIA_BLOB_PREFIX}x", png(), "image/png")
    assert (await read_stored_entry(f"{INBOUND_MEDIA_BLOB_PREFIX}x"))["kind"] == "binary"


# -- a deployment without the files extra -----------------------------------------------


@pytest.mark.usefixtures("files_extra_missing")
@pytest.mark.parametrize(("resource_id", "data"), [("a.txt", b"hello"), ("img/a.png", png()), ("missing.txt", None)])
async def test_without_the_files_extra_the_reader_is_the_501_before_any_storage_call(
    store: RecordingStorage, resource_id: str, data: bytes | None
) -> None:
    if data is not None:
        store.put(resource_id, data)
    with pytest.raises(NotSupportedError) as caught:
        await read_stored_entry(resource_id)
    assert caught.value.message == FILES_INSTALL_LINE
    assert caught.value.extra == {"code": "files-not-configured"}
    assert store.calls == []


@pytest.mark.usefixtures("files_extra_missing")
async def test_without_the_files_extra_an_unsafe_key_is_still_its_400(store: RecordingStorage) -> None:
    with pytest.raises(BadRequestError, match="non-canonical store key"):
        await read_stored_entry("a//b")
