"""Plugin-spec loading: the in-memory parser, the file loader, the wheel reader, and their loud failures."""

from __future__ import annotations

import io
import struct
import tracemalloc
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from tai42_kit.plugins import (
    MAX_DOCS_FILE_BYTES,
    MAX_DOCS_TOTAL_BYTES,
    MAX_PLUGIN_SPEC_BYTES,
    PLUGIN_SPEC_FILENAME,
    PluginDocsError,
    PluginSpecLoadError,
    load_plugin_spec,
    parse_plugin_spec,
    read_wheel_docs,
    read_wheel_plugin_spec,
    validate_docs,
)

_VALID_SPEC = """\
spec_version: 1
namespace: tai42
name: toolbox
package: tai42-toolbox
version: 0.1.0
description: "Generic tools and tool extensions."
license: Apache-2.0
repository: https://github.com/tai42ai/tai42/tree/main/plugins/toolbox
contract: ">=0.1,<0.2"
categories: [utilities]
tags: [uuid]
permissions:
  network: true
provides:
  - kind: tool
    name: generate_uuid
    module: tai42_toolbox.tools.generate_uuid
    description: "Generate a random UUID."
    tags: [uuid]
"""


def _write_spec(tmp_path: Path, text: str) -> Path:
    path = tmp_path / PLUGIN_SPEC_FILENAME
    path.write_text(text, encoding="utf-8")
    return path


def test_load_plugin_spec_returns_a_validated_spec(tmp_path: Path):
    spec = load_plugin_spec(_write_spec(tmp_path, _VALID_SPEC))
    assert spec.ref == "tai42/toolbox"
    assert spec.provides[0].module == "tai42_toolbox.tools.generate_uuid"


def test_load_missing_file_raises_load_error(tmp_path: Path):
    with pytest.raises(PluginSpecLoadError, match="cannot read"):
        load_plugin_spec(tmp_path / "absent.yml")


def test_load_invalid_yaml_raises_load_error(tmp_path: Path):
    with pytest.raises(PluginSpecLoadError, match="not valid YAML"):
        load_plugin_spec(_write_spec(tmp_path, "spec_version: [unclosed"))


def test_load_non_mapping_document_raises_load_error(tmp_path: Path):
    with pytest.raises(PluginSpecLoadError, match="expected a YAML mapping"):
        load_plugin_spec(_write_spec(tmp_path, "- just\n- a\n- list\n"))


def test_load_schema_violation_raises_validation_error(tmp_path: Path):
    # A well-formed mapping that breaks the schema surfaces pydantic's own
    # error (naming the field), never a wrapped or swallowed one.
    with pytest.raises(ValidationError, match="provides"):
        load_plugin_spec(_write_spec(tmp_path, _VALID_SPEC.split("provides:")[0]))


def test_parse_plugin_spec_validates_in_memory_bytes():
    # The shared low-level entry point: raw bytes in, validated spec out —
    # and undecodable bytes raise loudly.
    assert parse_plugin_spec(_VALID_SPEC.encode("utf-8")).ref == "tai42/toolbox"
    with pytest.raises(PluginSpecLoadError, match="UTF-8"):
        parse_plugin_spec(b"\xff\xfe\x00\x01")


def _write_wheel(tmp_path: Path, entries: dict[str, str]) -> Path:
    wheel = tmp_path / "tai42_toolbox-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return wheel


def _write_deflate_wheel(tmp_path: Path, content: bytes, *, lie_size: int | None = None) -> Path:
    """Write a wheel whose spec member is a real ZIP_DEFLATED entry.

    When ``lie_size`` is given, the entry's declared uncompressed size is
    overwritten in both the central directory and the local header to a value
    that does not match the member's real inflated size. ``ZipInfo.file_size`` is
    central-directory metadata an attacker fully controls, so a guard that trusts
    it is worthless.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}", content)
    raw = bytearray(buf.getvalue())
    if lie_size is not None:
        central = raw.find(b"PK\x01\x02")
        struct.pack_into("<I", raw, central + 24, lie_size)  # central dir uncompressed size
        local = raw.find(b"PK\x03\x04")
        struct.pack_into("<I", raw, local + 22, lie_size)  # local header uncompressed size
    wheel = tmp_path / "tai42_toolbox-0.1.0-py3-none-any.whl"
    wheel.write_bytes(bytes(raw))
    return wheel


def test_read_wheel_plugin_spec_reads_the_packaged_copy(tmp_path: Path):
    wheel = _write_wheel(tmp_path, {f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC})
    assert read_wheel_plugin_spec(wheel).package == "tai42-toolbox"


def test_read_wheel_finds_a_nested_packaged_copy(tmp_path: Path):
    # Namespace-packaged plugins ship the spec deeper than one directory.
    wheel = _write_wheel(tmp_path, {f"tai42_toolbox/nested/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC})
    assert read_wheel_plugin_spec(wheel).ref == "tai42/toolbox"


def test_read_wheel_without_spec_raises(tmp_path: Path):
    wheel = _write_wheel(tmp_path, {"tai42_toolbox/py.typed": ""})
    with pytest.raises(PluginSpecLoadError, match="contains no"):
        read_wheel_plugin_spec(wheel)


def test_read_wheel_with_several_specs_raises(tmp_path: Path):
    wheel = _write_wheel(
        tmp_path,
        {f"a/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC, f"b/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC},
    )
    with pytest.raises(PluginSpecLoadError, match="several"):
        read_wheel_plugin_spec(wheel)


def test_read_wheel_on_a_non_zip_raises(tmp_path: Path):
    junk = tmp_path / "not-a.whl"
    junk.write_text("plain text", encoding="utf-8")
    with pytest.raises(PluginSpecLoadError, match="cannot read wheel"):
        read_wheel_plugin_spec(junk)


def test_read_wheel_invalid_yaml_raises_load_error(tmp_path: Path):
    wheel = _write_wheel(tmp_path, {f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": "spec_version: [unclosed"})
    with pytest.raises(PluginSpecLoadError, match="not valid YAML"):
        read_wheel_plugin_spec(wheel)


# --- Untrusted-input hardening -------------------------------------------------


def test_parse_rejects_yaml_anchors_and_aliases():
    # Anchors/aliases are the billion-laughs expansion vector; a flat spec never
    # needs them, so encountering an anchor is refused outright.
    with pytest.raises(PluginSpecLoadError, match="anchors/aliases are not permitted"):
        parse_plugin_spec("spec_version: &v 1\nname: *v\n")
    # An alias encountered on its own (no preceding anchor) is refused too.
    with pytest.raises(PluginSpecLoadError, match="anchors/aliases are not permitted"):
        parse_plugin_spec("name: *ghost\n")


def test_parse_rejects_duplicate_mapping_keys():
    # A repeated key would silently win last-value; that ambiguity is rejected
    # and the offending key is named.
    doc = "spec_version: 1\nspec_version: 2\n"
    with pytest.raises(PluginSpecLoadError, match=r"duplicate mapping key.*spec_version"):
        parse_plugin_spec(doc)


def test_parse_rejects_over_cap_bytes():
    oversized = b"x" * (MAX_PLUGIN_SPEC_BYTES + 1)
    with pytest.raises(PluginSpecLoadError, match=f"exceeding the {MAX_PLUGIN_SPEC_BYTES}-byte limit"):
        parse_plugin_spec(oversized)


def test_parse_rejects_over_cap_str():
    oversized = "x" * (MAX_PLUGIN_SPEC_BYTES + 1)
    with pytest.raises(PluginSpecLoadError, match=f"exceeding the {MAX_PLUGIN_SPEC_BYTES}-byte limit"):
        parse_plugin_spec(oversized)


def test_parse_rejects_deeply_nested_document():
    # Deep flow nesting stays well under the byte cap yet exhausts the parser's
    # recursion, which is not a yaml.YAMLError and would otherwise escape.
    doc = "[" * 60_000
    assert len(doc.encode("utf-8")) < MAX_PLUGIN_SPEC_BYTES
    with pytest.raises(PluginSpecLoadError, match="nested too deeply"):
        parse_plugin_spec(doc)


def test_load_rejects_over_cap_file(tmp_path: Path):
    # Refused on the stat size before a single byte is read. Assert the
    # "plugin spec ... exceeding" fragment unique to the pre-read stat guard:
    # the downstream parse byte-cap emits "<path> is N bytes, exceeding"
    # (no "plugin spec " prefix), so removing the stat guard — which would
    # slurp a multi-gigabyte file into memory — fails this assertion.
    path = _write_spec(tmp_path, "x" * (MAX_PLUGIN_SPEC_BYTES + 1))
    with pytest.raises(
        PluginSpecLoadError,
        match=f"plugin spec .* bytes, exceeding the {MAX_PLUGIN_SPEC_BYTES}-byte limit",
    ):
        load_plugin_spec(path)


def test_load_non_utf8_file_raises_load_error(tmp_path: Path):
    # A non-UTF-8 tai-plugin.yml must raise PluginSpecLoadError, not let a raw
    # UnicodeDecodeError escape: load reads bytes and routes through
    # parse_plugin_spec, which enforces the UTF-8 decode uniformly.
    path = tmp_path / PLUGIN_SPEC_FILENAME
    path.write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(PluginSpecLoadError, match="not valid UTF-8"):
        load_plugin_spec(path)


def test_read_wheel_rejects_honest_oversize_member(tmp_path: Path):
    # An honestly-declared over-cap member: the bounded read pulls at most
    # MAX + 1 inflated bytes and the length check rejects it. Assert the
    # "inflates past" fragment unique to the bounded-read guard (the downstream
    # parse byte-cap says "bytes, exceeding", and the removed file_size guard
    # said "bytes uncompressed, exceeding"), so this pins the real guard.
    wheel = _write_deflate_wheel(tmp_path, b"x" * (MAX_PLUGIN_SPEC_BYTES + 1))
    with pytest.raises(
        PluginSpecLoadError,
        match=f"inflates past the {MAX_PLUGIN_SPEC_BYTES}-byte limit",
    ):
        read_wheel_plugin_spec(wheel)


def test_read_wheel_deflate_bomb_with_lying_size_stays_bounded(tmp_path: Path):
    # The real decompression-bomb attack: a ZIP_DEFLATED member that inflates to
    # tens of MiB while its declared uncompressed size is an attacker-controlled
    # lie. The size is lied UP well past the cap here so the bounded read — not an
    # early declared-EOF (which would trip a CRC mismatch before the guard) — is
    # what stops inflation, and the length guard rejects the member on real
    # inflated bytes. Matching "inflates past" pins that guard: delete it and the
    # bounded read still truncates to MAX + 1 bytes, but the rejection then comes
    # from parse_plugin_spec's byte cap ("exceeding"), failing this match.
    bomb_bytes = 32 * 1024 * 1024
    wheel = _write_deflate_wheel(tmp_path, b"\x00" * bomb_bytes, lie_size=16 * 1024 * 1024)

    tracemalloc.start()
    try:
        with pytest.raises(PluginSpecLoadError, match="inflates past"):
            read_wheel_plugin_spec(wheel)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Peak stays a few MiB, far under the 32 MiB payload. Reverting to the
    # file_size pre-check would let wheel.read() materialize the whole bomb
    # (peak in the tens of MiB), failing this bound — this is what makes the
    # test exercise the decompression bound rather than a declared size.
    assert peak < 8 * 1024 * 1024, f"peak {peak} bytes indicates the full bomb was inflated"


def _write_compressed_wheel(tmp_path: Path, content: bytes, compression: int) -> Path:
    """Write a wheel whose spec member uses ``compression`` for its stream."""
    wheel = tmp_path / "tai42_toolbox-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w", compression=compression) as zf:
        zf.writestr(f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}", content)
    return wheel


@pytest.mark.parametrize(
    ("label", "compression"),
    [("bzip2", zipfile.ZIP_BZIP2), ("lzma", zipfile.ZIP_LZMA)],
)
def test_read_wheel_rejects_non_deflate_compression_before_decompressing(tmp_path: Path, label: str, compression: int):
    # A BZIP2/LZMA member is a decompression bomb the bounded read cannot cap:
    # CPython's ZipExtFile threads the length cap into the decompressor only on
    # the deflate path, so bzip2/lzma inflate their whole block on the first
    # read() (a ~230-byte bzip2 / ~12 KiB lzma wheel expands to 80 MiB, peaking
    # ~190 MiB RSS). The compress_type guard refuses these before a byte is
    # read, so the bomb never materializes. tracemalloc pins that closure:
    # remove the guard and the length check would still reject the member, but
    # only after inflating the full 80 MiB — blowing this peak bound.
    bomb_bytes = 80 * 1024 * 1024
    wheel = _write_compressed_wheel(tmp_path, b"\x00" * bomb_bytes, compression)
    assert wheel.stat().st_size < MAX_PLUGIN_SPEC_BYTES  # tiny on disk; huge inflated

    tracemalloc.start()
    try:
        with pytest.raises(PluginSpecLoadError, match="unsupported compression"):
            read_wheel_plugin_spec(wheel)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 8 * 1024 * 1024, f"{label} member: peak {peak} bytes means the bomb was decompressed before rejection"


def _write_corrupted_deflate_wheel(tmp_path: Path, content: bytes) -> Path:
    """Write a ZIP_DEFLATED wheel whose deflate stream is corrupted mid-payload.

    Bytes are flipped a few positions into the compressed data (past the deflate
    block header), so ``ZipExtFile.read`` raises a raw ``zlib.error`` while
    inflating — the corruption breaks the deflate structure itself, not just the
    trailing CRC. ``zlib.error`` is not an ``OSError`` subclass, so it only maps
    to ``PluginSpecLoadError`` if the reader names it in its except tuple.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}", content)
    raw = bytearray(buf.getvalue())
    local = raw.find(b"PK\x03\x04")
    name_len = struct.unpack_from("<H", raw, local + 26)[0]
    extra_len = struct.unpack_from("<H", raw, local + 28)[0]
    data_start = local + 30 + name_len + extra_len
    for i in range(data_start + 3, data_start + 13):
        raw[i] ^= 0xFF
    wheel = tmp_path / "tai42_toolbox-0.1.0-py3-none-any.whl"
    wheel.write_bytes(bytes(raw))
    return wheel


def test_read_wheel_mid_stream_deflate_corruption_raises_load_error(tmp_path: Path):
    # Mid-stream deflate corruption surfaces as a raw zlib.error during the
    # member read; zlib.error does not subclass OSError, so it escapes the reader
    # uncaught unless the except tuple names it — violating the docstring promise
    # that an unreadable archive raises PluginSpecLoadError. Remove zlib.error
    # from the tuple and this test fails with an unwrapped zlib.error.
    wheel = _write_corrupted_deflate_wheel(tmp_path, b"key: value\n" * 2000)
    with pytest.raises(PluginSpecLoadError, match="cannot read wheel"):
        read_wheel_plugin_spec(wheel)


def test_read_wheel_encrypted_member_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # An encrypted member surfaces as RuntimeError when the member is opened,
    # which is not a subclass of (BadZipFile, OSError) but still means
    # "unreadable" — the reader opens the member for the bounded read.
    wheel = _write_wheel(tmp_path, {f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC})

    def _encrypted(self: zipfile.ZipFile, name: str, *args: object, **kwargs: object) -> object:
        raise RuntimeError("File is encrypted, password required for extraction")

    monkeypatch.setattr(zipfile.ZipFile, "open", _encrypted)
    with pytest.raises(PluginSpecLoadError, match="cannot read wheel"):
        read_wheel_plugin_spec(wheel)


def test_read_wheel_unsupported_compression_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # An unsupported compression method surfaces as NotImplementedError when the
    # member is opened, also outside (BadZipFile, OSError) yet still an
    # unreadable archive.
    wheel = _write_wheel(tmp_path, {f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC})

    def _unsupported(self: zipfile.ZipFile, name: str, *args: object, **kwargs: object) -> object:
        raise NotImplementedError("compression type 99 (AES)")

    monkeypatch.setattr(zipfile.ZipFile, "open", _unsupported)
    with pytest.raises(PluginSpecLoadError, match="cannot read wheel"):
        read_wheel_plugin_spec(wheel)


# --- Wheel docs reader ---------------------------------------------------------

_INDEX_MDX = """\
---
title: Toolbox
description: Generic tools.
---

# Toolbox

See the [guide](guide.mdx) and ![logo](images/logo.png).
"""

_GUIDE_MDX = """\
---
title: Guide
description: How to use the toolbox.
---

Back to [home](index.mdx).
"""

_MINIMAL_INDEX = """\
---
title: Home
description: The landing page.
---

# Home
"""


def _write_docs_wheel(tmp_path: Path, entries: dict[str, str | bytes]) -> Path:
    """A wheel carrying a spec and (usually) a ``docs/`` tree beside it."""
    wheel = tmp_path / "tai42_toolbox-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return wheel


def _page(body: str, *, title: str = "T", description: str = "D") -> bytes:
    return f"---\ntitle: {title}\ndescription: {description}\n---\n\n{body}\n".encode()


def test_read_wheel_docs_reads_the_packaged_tree(tmp_path: Path):
    wheel = _write_docs_wheel(
        tmp_path,
        {
            f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC,
            "tai42_toolbox/docs/index.mdx": _INDEX_MDX,
            "tai42_toolbox/docs/guide.mdx": _GUIDE_MDX,
            "tai42_toolbox/docs/images/logo.png": b"\x89PNG\r\n\x1a\n binary",
        },
    )
    docs = read_wheel_docs(wheel)
    assert set(docs) == {"docs/index.mdx", "docs/guide.mdx", "docs/images/logo.png"}
    assert docs["docs/guide.mdx"] == _GUIDE_MDX.encode()
    # The extracted tree is a valid third-party set: refs resolve, no unsafe mdx.
    validate_docs(docs, first_party=False)


def test_read_wheel_docs_missing_tree_raises(tmp_path: Path):
    wheel = _write_docs_wheel(tmp_path, {f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC})
    with pytest.raises(PluginDocsError, match="packages no docs/ tree"):
        read_wheel_docs(wheel)


def test_read_wheel_docs_without_spec_raises(tmp_path: Path):
    wheel = _write_docs_wheel(tmp_path, {"tai42_toolbox/docs/index.mdx": _INDEX_MDX})
    with pytest.raises(PluginDocsError, match="contains no"):
        read_wheel_docs(wheel)


def test_read_wheel_docs_traversing_member_raises(tmp_path: Path):
    wheel = _write_docs_wheel(
        tmp_path,
        {
            f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC,
            "tai42_toolbox/docs/index.mdx": _INDEX_MDX,
            "tai42_toolbox/docs/../escape.mdx": "x",
        },
    )
    with pytest.raises(PluginDocsError, match="unsafe"):
        read_wheel_docs(wheel)


def test_read_wheel_docs_backslash_traversing_member_raises(tmp_path: Path):
    # A backslash-separated ``..`` is a single forward-slash segment, so a member
    # like ``docs/sub\..\..\evil.mdx`` would escape a ``/``-only split; the guard
    # splits on both separators and rejects it.
    wheel = _write_docs_wheel(
        tmp_path,
        {
            f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC,
            "tai42_toolbox/docs/index.mdx": _INDEX_MDX,
            "tai42_toolbox/docs/sub\\..\\..\\evil.mdx": "x",
        },
    )
    with pytest.raises(PluginDocsError, match="unsafe"):
        read_wheel_docs(wheel)


def test_read_wheel_docs_per_file_ceiling_raises(tmp_path: Path):
    wheel = _write_docs_wheel(
        tmp_path,
        {
            f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC,
            "tai42_toolbox/docs/index.mdx": _INDEX_MDX,
            "tai42_toolbox/docs/big.mdx": "x" * (MAX_DOCS_FILE_BYTES + 1),
        },
    )
    with pytest.raises(PluginDocsError, match="per-file limit"):
        read_wheel_docs(wheel)


def test_read_wheel_docs_total_ceiling_raises(tmp_path: Path):
    # Each member sits at the per-file cap; enough of them cross the total cap.
    page = "x" * MAX_DOCS_FILE_BYTES
    entries: dict[str, str | bytes] = {f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}": _VALID_SPEC}
    for i in range(MAX_DOCS_TOTAL_BYTES // MAX_DOCS_FILE_BYTES + 1):
        entries[f"tai42_toolbox/docs/page{i}.mdx"] = page
    wheel = _write_docs_wheel(tmp_path, entries)
    with pytest.raises(PluginDocsError, match="total limit"):
        read_wheel_docs(wheel)


def _write_bzip2_docs_wheel(tmp_path: Path, content: bytes) -> Path:
    wheel = tmp_path / "tai42_toolbox-0.1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as zf:
        zf.writestr(f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}", _VALID_SPEC)
        info = zipfile.ZipInfo("tai42_toolbox/docs/index.mdx")
        info.compress_type = zipfile.ZIP_BZIP2
        zf.writestr(info, content)
    return wheel


def test_read_wheel_docs_rejects_non_deflate_member_before_decompressing(tmp_path: Path):
    # A BZIP2 docs member is a bomb the bounded read cannot cap (the length cap is
    # threaded into the decompressor only on the deflate path). The compress_type
    # guard refuses it before a byte is read, so the 80 MiB never materializes.
    wheel = _write_bzip2_docs_wheel(tmp_path, b"\x00" * (80 * 1024 * 1024))
    assert wheel.stat().st_size < MAX_DOCS_FILE_BYTES  # tiny on disk, huge inflated

    tracemalloc.start()
    try:
        with pytest.raises(PluginDocsError, match="unsupported compression"):
            read_wheel_docs(wheel)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f"peak {peak} bytes means the bomb was decompressed before rejection"


def _write_deflate_docs_wheel(tmp_path: Path, content: bytes, *, lie_size: int) -> Path:
    """A wheel whose docs member is a ZIP_DEFLATED entry lying about its size.

    The member is written FIRST so the first local/central headers are its own,
    then its declared uncompressed size is overwritten to a value that does not
    match its real inflated size — the metadata a ``ZipInfo.file_size``-trusting
    reader would wrongly trust.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("tai42_toolbox/docs/index.mdx", content)
        zf.writestr(f"tai42_toolbox/{PLUGIN_SPEC_FILENAME}", _VALID_SPEC)
    raw = bytearray(buf.getvalue())
    central = raw.find(b"PK\x01\x02")
    struct.pack_into("<I", raw, central + 24, lie_size)
    local = raw.find(b"PK\x03\x04")
    struct.pack_into("<I", raw, local + 22, lie_size)
    wheel = tmp_path / "tai42_toolbox-0.1.0-py3-none-any.whl"
    wheel.write_bytes(bytes(raw))
    return wheel


def test_read_wheel_docs_deflate_bomb_with_lying_size_stays_bounded(tmp_path: Path):
    # A ZIP_DEFLATED docs member inflating to 32 MiB while its declared size is an
    # attacker-controlled lie. The size is lied UP past the per-file cap so the
    # bounded read — not an early declared-EOF (which would CRC-fault before the
    # guard) — caps inflation at MAX + 1 bytes and the guard rejects on real
    # inflated bytes. Matching "per-file limit" pins that guard: delete it and the
    # over-cap member is silently accepted (its MAX + 1 bytes sit under the total
    # cap), so read_wheel_docs would return rather than raise.
    wheel = _write_deflate_docs_wheel(tmp_path, b"\x00" * (32 * 1024 * 1024), lie_size=16 * 1024 * 1024)

    tracemalloc.start()
    try:
        with pytest.raises(PluginDocsError, match="per-file limit"):
            read_wheel_docs(wheel)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024, f"peak {peak} bytes indicates the full bomb was inflated"


# --- Canonical docs validation -------------------------------------------------


def test_validate_docs_accepts_a_minimal_first_and_third_party_tree():
    docs = {"docs/index.mdx": _MINIMAL_INDEX.encode()}
    validate_docs(docs, first_party=True)
    validate_docs(docs, first_party=False)


def test_validate_docs_rejects_missing_index():
    with pytest.raises(PluginDocsError, match=r"index\.mdx is missing"):
        validate_docs({"docs/guide.mdx": _MINIMAL_INDEX.encode()}, first_party=False)


def test_validate_docs_rejects_svg_for_every_namespace():
    docs = {"docs/index.mdx": _MINIMAL_INDEX.encode(), "docs/images/diagram.svg": b"<svg/>"}
    # SVG is an XSS surface refused even first-party, at this earliest gate.
    with pytest.raises(PluginDocsError, match="raster images"):
        validate_docs(docs, first_party=True)


def test_validate_docs_rejects_non_mdx_page():
    docs = {"docs/index.mdx": _MINIMAL_INDEX.encode(), "docs/page.html": b"<html></html>"}
    with pytest.raises(PluginDocsError, match=r"only \.mdx pages"):
        validate_docs(docs, first_party=True)


def test_validate_docs_rejects_raster_outside_images():
    docs = {"docs/index.mdx": _MINIMAL_INDEX.encode(), "docs/logo.png": b"binary"}
    with pytest.raises(PluginDocsError, match=r"only \.mdx pages"):
        validate_docs(docs, first_party=True)


def test_validate_docs_rejects_missing_front_matter():
    with pytest.raises(PluginDocsError, match="front-matter block"):
        validate_docs({"docs/index.mdx": b"# no front matter\n"}, first_party=False)


def test_validate_docs_rejects_extra_front_matter_key():
    page = b"---\ntitle: T\ndescription: D\nextra: x\n---\n\nbody\n"
    with pytest.raises(PluginDocsError, match="exactly `title` and `description`"):
        validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_rejects_non_string_front_matter_value():
    page = b"---\ntitle: 123\ndescription: D\n---\n\nbody\n"
    with pytest.raises(PluginDocsError, match="must be a plain string"):
        validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_rejects_empty_front_matter_value():
    # A quoted empty title parses to "" — rejected before the metacharacter check.
    page = b'---\ntitle: ""\ndescription: D\n---\n\nbody\n'
    with pytest.raises(PluginDocsError, match="must not be empty"):
        validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_rejects_multiline_front_matter_value():
    page = b"---\ntitle: |\n  line one\n  line two\ndescription: D\n---\n\nbody\n"
    with pytest.raises(PluginDocsError, match="single line"):
        validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_rejects_fence_metacharacter_front_matter_value():
    page = b"---\ntitle: it's a tool\ndescription: D\n---\n\nbody\n"
    with pytest.raises(PluginDocsError, match="quotes, backslashes"):
        validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_rejects_unresolvable_link():
    with pytest.raises(PluginDocsError, match="not in the docs set"):
        validate_docs({"docs/index.mdx": _page("See [x](missing.mdx).")}, first_party=False)


def test_validate_docs_rejects_unresolvable_image():
    with pytest.raises(PluginDocsError, match="not in the docs set"):
        validate_docs({"docs/index.mdx": _page("![x](images/missing.png)")}, first_party=False)


def test_validate_docs_ignores_references_inside_code(tmp_path: Path):
    # A link shown in a code sample is documentation, not a live reference — the
    # code-masking pass keeps it out of resolution.
    page = _page("```\n[x](missing.mdx)\n```")
    validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_rejects_javascript_scheme_third_party():
    with pytest.raises(PluginDocsError, match="scheme"):
        validate_docs({"docs/index.mdx": _page("[x](javascript:alert(1))")}, first_party=False)


def test_validate_docs_rejects_data_scheme_third_party():
    page = _page("![x](data:text/html;base64,PHN2Zz4=)")
    with pytest.raises(PluginDocsError, match="scheme"):
        validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_allows_http_scheme_third_party():
    validate_docs({"docs/index.mdx": _page("[x](https://example.com/page)")}, first_party=False)


def test_validate_docs_allows_other_schemes_for_first_party():
    # First-party callers are trusted; the scheme allow-list is third-party only.
    validate_docs({"docs/index.mdx": _page("[mail](mailto:a@b.com)")}, first_party=True)


def test_validate_docs_allows_autolink_third_party():
    # A ``<https://…>`` autolink is not a raw tag and must not trip the whitelist.
    validate_docs({"docs/index.mdx": _page("See <https://example.com> for more.")}, first_party=False)


def test_validate_docs_whitelist_first_party_versus_third_party():
    # Identical input: a raw <script> tag is refused third-party, allowed first-party.
    page = _page("<script>alert(1)</script>")
    with pytest.raises(PluginDocsError, match="HTML/JSX tag"):
        validate_docs({"docs/index.mdx": page}, first_party=False)
    validate_docs({"docs/index.mdx": page}, first_party=True)


def test_validate_docs_rejects_mdx_esm_import_third_party():
    page = _page("import Chart from './chart.js'")
    with pytest.raises(PluginDocsError, match="import"):
        validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_rejects_reference_definition_javascript_scheme():
    # A reference-DEFINITION destination must run the same scheme gate as an
    # inline target — otherwise ``[x]: javascript:…`` slips past the inline scan.
    page = _page("[x][ref]\n\n[ref]: javascript:alert(1)")
    with pytest.raises(PluginDocsError, match="scheme"):
        validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_allows_site_root_reference_first_party():
    # A first-party page may link to central docs pages by site-root route; these
    # resolve against the unified docs site, not this isolated set.
    page = _page("See [x](/concepts/connectors) and [y](/operate/accounts#oauth).")
    validate_docs({"docs/index.mdx": page}, first_party=True)


def test_validate_docs_rejects_site_root_reference_third_party():
    with pytest.raises(PluginDocsError, match="site-root-relative"):
        validate_docs({"docs/index.mdx": _page("[x](/concepts/connectors)")}, first_party=False)


def test_validate_docs_rejects_protocol_relative_reference_first_party():
    # A ``//host`` protocol-relative target is not a site-root route and must not
    # slip through as one, even for first-party.
    with pytest.raises(PluginDocsError, match="not in the docs set"):
        validate_docs({"docs/index.mdx": _page("[x](//evil.example/path)")}, first_party=True)


def test_validate_docs_rejects_reference_definition_offset_relative():
    # A reference-definition destination pointing outside the docs set is caught.
    page = _page("[x][ref]\n\n[ref]: missing.mdx")
    with pytest.raises(PluginDocsError, match="not in the docs set"):
        validate_docs({"docs/index.mdx": page}, first_party=False)


def test_validate_docs_allows_reference_definition_in_set():
    docs = {
        "docs/index.mdx": _page("[home][ref]\n\n[ref]: index.mdx"),
    }
    validate_docs(docs, first_party=False)


def test_validate_docs_rejects_raw_mdx_expression_third_party():
    # An unescaped ``{`` opens an MDX expression (evaluated JS); rejected for the
    # plain-markdown subset even inside a ``{/* comment */}``.
    with pytest.raises(PluginDocsError, match="MDX expression"):
        validate_docs({"docs/index.mdx": _page("Total is {1 + 1}.")}, first_party=False)
    with pytest.raises(PluginDocsError, match="MDX expression"):
        validate_docs({"docs/index.mdx": _page("{/* comment */}")}, first_party=False)


def test_validate_docs_allows_escaped_brace_and_first_party_expression():
    # A backslash-escaped ``\{`` is a literal brace, allowed third-party; and the
    # expression subset is exempt for trusted first-party authors.
    validate_docs({"docs/index.mdx": _page(r"A literal \{ brace.")}, first_party=False)
    validate_docs({"docs/index.mdx": _page("Total is {1 + 1}.")}, first_party=True)


def test_validate_docs_rejects_even_backslash_brace_third_party():
    # Escaping is by backslash parity: ``\\{`` is an escaped backslash followed by a
    # LIVE ``{`` (even run), so the expression is rejected — while an odd run ``\{``
    # escapes the brace to a literal and is allowed.
    with pytest.raises(PluginDocsError, match="MDX expression"):
        validate_docs({"docs/index.mdx": _page(r"Value \\{alert(1)}")}, first_party=False)
    validate_docs({"docs/index.mdx": _page(r"A \{literal} brace.")}, first_party=False)


def test_validate_docs_allows_gfm_footnote_definition():
    # A GFM footnote definition (``[^1]: text``) carries a body, not a URL
    # destination, so it must not be read as a reference definition and have its
    # first body word flagged as an unresolved reference — in either namespace.
    docs = {"docs/index.mdx": _page("A claim.[^1]\n\n[^1]: This is the note.")}
    validate_docs(docs, first_party=False)
    validate_docs(docs, first_party=True)
