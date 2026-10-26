"""The type-detect + dispatch registry: Magika-driven dispatch, bytes->temp-file
materialization for path loaders, the url-suffix fallback, media pass-through, and
the loud raise on an unknown type. Magika is faked so dispatch is deterministic."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastmcp.utilities.types import Audio, Image

from tai42_skeleton.operations import NotSupportedError
from tai42_skeleton.template import ContentNotLoadableError
from tai42_skeleton.template import file_loading as fl

from .._fakes.content_fixtures import (
    CSV,
    FILES_INSTALL_LINE,
    HTML,
    JAVASCRIPT,
    LOCKED_PDF_MESSAGE,
    NOTES_PDF,
    RAGGED_CSV,
    RAGGED_CSV_MESSAGE,
    SQL,
    SQL_LATIN1,
    TEMPLATED_HTML,
    large_json,
    large_xlsx,
    locked_pdf,
    png,
    zip_archive,
)


class _FakeMagika:
    def __init__(self, label: str, mime: str, extensions: list[str], is_text: bool = False) -> None:
        self._output = SimpleNamespace(label=label, mime_type=mime, extensions=extensions, is_text=is_text)

    def identify_bytes(self, data: bytes) -> SimpleNamespace:
        return SimpleNamespace(output=self._output)


def _fake_magika(label: str, mime: str, extensions: list[str], is_text: bool = False):
    return lambda: _FakeMagika(label, mime, extensions, is_text)


def test_detect_reads_label_mime_and_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("txt", "text/plain", ["txt"]))
    assert fl.detect(b"hello", None) == ("txt", "text/plain", ".txt")


def test_detect_falls_back_to_url_suffix_when_unclassified(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("unknown", "application/octet-stream", []))
    _label, _mime, ext = fl.detect(b"\x00\x01", "https://example.com/report.pdf?token=abc")
    assert ext == ".pdf"


def test_registry_materializes_bytes_to_temp_file_for_path_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _FakeLoader:
        def __init__(self, path: str) -> None:
            captured["path"] = path
            captured["exists"] = os.path.exists(path)
            with open(path, "rb") as handle:
                captured["content"] = handle.read()

        def load(self) -> list[SimpleNamespace]:
            return [SimpleNamespace(page_content="PAGE ONE"), SimpleNamespace(page_content="PAGE TWO")]

    monkeypatch.setattr(fl, "PyPDFLoader", _FakeLoader)
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("pdf", "application/pdf", ["pdf"]))

    result = fl.load_content(b"%PDF-1.4 raw", "https://example.com/report.pdf")

    assert result == "PAGE ONE\nPAGE TWO"
    assert captured["exists"] is True  # the loader saw a real file on disk
    assert captured["content"] == b"%PDF-1.4 raw"
    assert str(captured["path"]).endswith(".pdf")
    assert not os.path.exists(str(captured["path"]))  # cleaned up after


def test_registry_honors_url_suffix_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    # Magika can't classify -> the url suffix (.txt) decides, routing to the
    # plain-text handler which decodes the bytes directly.
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("unknown", "application/octet-stream", []))
    result = fl.load_content(b"plain body text", "https://example.com/notes.txt?token=1")
    assert result == "plain body text"


def test_registry_returns_image_media_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("png", "image/png", ["png"]))
    result = fl.load_content(b"\x89PNG\r\n", "logo.png")
    assert isinstance(result, Image)


def test_registry_returns_audio_media_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("wav", "audio/wav", ["wav"]))
    result = fl.load_content(b"RIFF....WAVE", "clip.wav")
    assert isinstance(result, Audio)


def test_registry_raises_on_unknown_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("unknown", "application/x-weird", []))
    with pytest.raises(ContentNotLoadableError, match="Unsupported file type"):
        fl.load_content(b"\x00\x01\x02", None)


def test_plain_text_strict_decode_raises_on_non_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    # A text-classified but non-utf-8 payload raises loudly rather than corrupting.
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("txt", "text/plain", ["txt"]))
    with pytest.raises(ContentNotLoadableError, match=r"'note\.txt' is detected as text but is not UTF-8") as excinfo:
        fl.load_content(b"\xff\xfe not utf-8", "note.txt")
    assert isinstance(excinfo.value.__cause__, UnicodeDecodeError)


def test_detect_mime_returns_magika_mime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("png", "image/png", ["png"]))
    assert fl.detect_mime(b"\x89PNG") == "image/png"


def test_load_content_rejects_over_cap_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Untrusted document bytes over ``FILE_LOADING_MAX_BYTES`` raise loudly
    BEFORE any detection/parse (a storage-id source bypasses fetch_url's cap)."""
    monkeypatch.setattr(fl, "file_loading_settings", lambda: SimpleNamespace(max_bytes=8))
    # A guard failure means detection never runs; a magika call here would be a bug.
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("txt", "text/plain", ["txt"]))
    with pytest.raises(ContentNotLoadableError, match="FILE_LOADING_MAX_BYTES"):
        fl.load_content(b"way too many bytes", "big.txt")


def test_load_content_allows_at_cap_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bytes exactly at the cap are allowed (the bound is strictly greater-than)."""
    monkeypatch.setattr(fl, "file_loading_settings", lambda: SimpleNamespace(max_bytes=5))
    monkeypatch.setattr(fl, "_magika_client", _fake_magika("txt", "text/plain", ["txt"]))
    assert fl.load_content(b"hello", "ok.txt") == "hello"


# -- the one text, document or binary rule -------------------------------------


def _magika_label(data: bytes) -> tuple[str, bool]:
    output = fl._magika_client().identify_bytes(data).output
    return output.label, bool(output.is_text)


@pytest.mark.parametrize("build", [large_xlsx, lambda: png(200, 60), large_json])
def test_the_two_detection_windows_classify_an_object_as_the_whole_does(build) -> None:
    data = build()
    assert len(data) > 2 * fl.DETECTION_WINDOW_BYTES
    window = fl.DETECTION_WINDOW_BYTES
    sample = data[:window] + data[-window:]
    assert fl.detect(sample, None) == fl.detect(data, None)
    assert fl.classify(sample, None) == fl.classify(data, None)


def test_the_detection_window_is_the_models_block_size() -> None:
    assert fl._magika_client()._model_config.block_size == fl.DETECTION_WINDOW_BYTES


def test_a_text_type_with_no_handler_loads_as_its_utf8_text() -> None:
    assert _magika_label(SQL) == ("sql", True)
    assert fl.classify(SQL, "queries/lookup.sql").kind == "text"
    assert fl.load_content(SQL, "queries/lookup.sql") == SQL.decode()
    assert fl.classify(JAVASCRIPT, "app.js").kind == "text"
    assert fl.load_content(JAVASCRIPT, "app.js") == JAVASCRIPT.decode()


def test_non_utf8_bytes_of_a_text_type_raise_the_strict_decode_error() -> None:
    assert _magika_label(SQL_LATIN1) == ("sql", True)
    with pytest.raises(ContentNotLoadableError, match=r"'q\.sql' is detected as text but is not UTF-8"):
        fl.load_content(SQL_LATIN1, "q.sql")


def test_empty_bytes_are_empty_text_without_calling_magika(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse() -> None:
        raise AssertionError("Magika is not called for empty bytes")

    monkeypatch.setattr(fl, "_magika_client", _refuse)
    assert fl.load_content(b"", "x") == ""
    assert fl.classify(b"", "x").kind == "text"


def test_a_zip_stays_binary_and_unsupported() -> None:
    archive = zip_archive()
    assert fl.classify(archive, "a.zip").kind == "binary"
    with pytest.raises(ContentNotLoadableError, match="Unsupported file type"):
        fl.load_content(archive, "a.zip")


@pytest.mark.parametrize("text", ["situação\n", "Ångström\n", "Straße\n", "garçon\n"])
def test_short_utf8_text_the_detector_cannot_name_is_text(text: str) -> None:
    data = text.encode()
    assert _magika_label(data) == ("unknown", False)
    assert fl.classify(data, "parts/greeting").kind == "text"
    assert fl.load_content(data, "parts/greeting") == text


def _undetected(monkeypatch: pytest.MonkeyPatch) -> None:
    detection = fl.Detection("unknown", "application/octet-stream", "", False)
    monkeypatch.setattr(fl, "_identify", lambda data, source: detection)


@pytest.mark.parametrize("data", [b"ab\x00cd", "caf\xe9".encode("latin-1"), b"\xc3"])
def test_undetected_bytes_that_are_not_utf8_text_stay_binary(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    _undetected(monkeypatch)
    assert fl.classify(data, "blob").kind == "binary"


@pytest.mark.parametrize(
    "text",
    ["a" + "ç" * fl.DETECTION_WINDOW_BYTES, "ç" * fl.DETECTION_WINDOW_BYTES + "a"],
    ids=["cut-at-the-first-windows-end", "cut-at-the-last-windows-start"],
)
def test_undetected_text_cut_where_its_two_windows_join_is_text(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    _undetected(monkeypatch)
    window = fl.DETECTION_WINDOW_BYTES
    data = text.encode()
    sample = data[:window] + data[-window:]
    classified = fl.classify(sample, "parts/long")
    assert classified.kind == "text"
    assert fl.load_classified(data, classified, "parts/long") == text


def test_a_text_label_with_no_handler_takes_the_text_path_for_both_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fl, "_identify", lambda data, source: fl.Detection("toml", "application/toml", ".toml", True))
    assert fl.classify(b"a = 1", "c.toml").kind == "text"
    assert fl.load_content(b"a = 1", "c.toml") == "a = 1"


def test_a_pdf_is_a_document_whose_content_is_its_extracted_text() -> None:
    assert _magika_label(NOTES_PDF) == ("pdf", False)
    assert fl.classify(NOTES_PDF, "docs/notes.pdf").kind == "document"
    content = fl.load_content(NOTES_PDF, "docs/notes.pdf")
    assert isinstance(content, str)
    assert "Quarterly notes" in content


def test_csv_and_html_are_text_entries() -> None:
    assert fl.classify(CSV, "data/a.csv").kind == "text"
    assert _magika_label(HTML) == ("html", True)
    assert fl.classify(HTML, "page.html").kind == "text"
    content = fl.load_content(HTML, "page.html")
    assert isinstance(content, str)
    assert "<h1>" not in content


def test_an_html_template_labelled_twig_loads_its_source_as_text() -> None:
    assert _magika_label(TEMPLATED_HTML) == ("twig", True)
    assert fl.classify(TEMPLATED_HTML, "page.html").kind == "text"
    assert fl.load_content(TEMPLATED_HTML, "page.html") == TEMPLATED_HTML.decode()


@pytest.mark.parametrize(("is_text", "kind"), [(False, "document"), (True, "text")])
def test_an_extracting_handler_is_a_document_unless_the_bytes_are_text(
    monkeypatch: pytest.MonkeyPatch, is_text: bool, kind: str
) -> None:
    monkeypatch.setattr(fl, "_identify", lambda data, source: fl.Detection("pdf", "application/pdf", ".pdf", is_text))
    assert fl.classify(b"%PDF", "a.pdf").kind == kind


# -- an extractor's failure on the stored bytes ---------------------------------


def test_an_encrypted_pdf_is_refused_with_the_parsers_error_as_cause() -> None:
    from pypdf.errors import FileNotDecryptedError

    data = locked_pdf()
    assert _magika_label(data) == ("pdf", False)
    with pytest.raises(ContentNotLoadableError) as caught:
        fl.load_content(data, "docs/locked.pdf")
    assert str(caught.value) == LOCKED_PDF_MESSAGE
    assert isinstance(caught.value.__cause__, FileNotDecryptedError)


def test_a_ragged_csv_is_refused_with_the_parsers_error_as_cause() -> None:
    from pandas.errors import ParserError

    assert _magika_label(RAGGED_CSV) == ("csv", True)
    with pytest.raises(ContentNotLoadableError) as caught:
        fl.load_content(RAGGED_CSV, "data/scores.csv")
    assert str(caught.value) == RAGGED_CSV_MESSAGE
    assert isinstance(caught.value.__cause__, ParserError)


def test_a_truncated_pdf_names_the_stream_error() -> None:
    with pytest.raises(ContentNotLoadableError, match="PdfStreamError"):
        fl.load_content(NOTES_PDF[:300], "docs/cut.pdf")


def _patch_pdf_loader(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    def _raise(data: bytes, mime: str | None, source: str | None) -> str:
        raise error

    registry = tuple(
        fl.Handler(h.labels, h.mime_substrings, h.extensions, _raise, h.kind) if "pdf" in h.labels else h
        for h in fl._REGISTRY
    )
    monkeypatch.setattr(fl, "_REGISTRY", registry)


@pytest.mark.parametrize("error", [OSError("disk"), ContentNotLoadableError("x")])
def test_a_host_fault_or_a_refusal_from_the_loader_propagates_unchanged(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    _patch_pdf_loader(monkeypatch, error)
    with pytest.raises(type(error)) as caught:
        fl.load_content(NOTES_PDF, "docs/notes.pdf")
    assert caught.value is error


def test_an_error_with_no_text_ends_the_message_with_its_class(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_pdf_loader(monkeypatch, ValueError())
    with pytest.raises(ContentNotLoadableError) as caught:
        fl.load_content(NOTES_PDF, "docs/notes.pdf")
    assert str(caught.value) == "'docs/notes.pdf' could not be extracted as pdf: ValueError"


# -- a deployment without the files extra ---------------------------------------


@pytest.mark.usefixtures("files_extra_missing")
def test_importing_the_module_without_the_files_extra_is_the_typed_501_every_time() -> None:
    import importlib

    for _ in range(2):
        with pytest.raises(NotSupportedError) as caught:
            importlib.import_module("tai42_skeleton.template.file_loading")
        assert caught.value.message == FILES_INSTALL_LINE
        assert caught.value.extra == {"code": "files-not-configured"}
        assert caught.value.status == 501
        assert isinstance(caught.value.__cause__, ImportError)
