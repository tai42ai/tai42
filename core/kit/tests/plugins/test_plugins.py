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
    MAX_PLUGIN_SPEC_BYTES,
    PLUGIN_SPEC_FILENAME,
    PluginSpecLoadError,
    load_plugin_spec,
    parse_plugin_spec,
    read_wheel_plugin_spec,
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
    overwritten in both the central directory and the local header — the
    zip-bomb attack: a tiny declared size hiding a member that inflates far past
    it. ``ZipInfo.file_size`` is central-directory metadata an attacker fully
    controls, so a guard that trusts it is worthless.
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
    # tens of MiB while lying that its uncompressed size is 50 bytes. The removed
    # ZipInfo.file_size pre-check waved this through (50 < cap) and then
    # wheel.read() expanded the whole bomb into memory. The bounded read caps
    # inflation at MAX + 1 bytes, so the bomb never fully materializes, and the
    # truncated read trips a CRC mismatch that surfaces as PluginSpecLoadError.
    bomb_bytes = 32 * 1024 * 1024
    wheel = _write_deflate_wheel(tmp_path, b"\x00" * bomb_bytes, lie_size=50)

    tracemalloc.start()
    try:
        with pytest.raises(PluginSpecLoadError):
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
