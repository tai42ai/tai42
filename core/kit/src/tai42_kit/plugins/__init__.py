"""Plugin-spec loading: the YAML I/O for ``tai-plugin.yml``.

Every installable TAI plugin ships a ``tai-plugin.yml`` — at its repo root and
as package-data inside the built wheel. The schema is
:class:`tai42_contract.plugins.PluginSpec`; these helpers do the YAML reading the
pure contract cannot (the contract has no YAML dependency) and validate loudly.
:func:`parse_plugin_spec` is the shared low-level entry point — it validates
spec content already in memory (``bytes`` or ``str``), and both loaders route
through it.

Because a spec may arrive as untrusted network input (an artifact fetched from
a registry, a wheel of unknown provenance), parsing is hardened against
resource-exhaustion and ambiguity attacks: content larger than
:data:`MAX_PLUGIN_SPEC_BYTES` is rejected before parsing; YAML anchors/aliases
(the billion-laughs expansion vector) and duplicate mapping keys (silent
last-value-wins) are rejected outright; deeply nested documents that exhaust the
interpreter stack are caught. A flat ``tai-plugin.yml`` needs none of those
constructs, so rejecting them costs nothing.

Failure surface: content- and file-level problems (over-size input, missing
file, non-UTF-8 bytes, unparseable YAML, anchors/aliases, duplicate keys,
excessive nesting, a non-mapping document, a wheel with zero or several spec
files, an unreadable archive) raise :class:`PluginSpecLoadError`; schema
violations inside a well-formed document propagate pydantic's
``ValidationError``, which already names every offending field. Nothing
degrades silently.
"""

from __future__ import annotations

import zipfile
import zlib
from pathlib import Path
from typing import Any

import yaml
from tai42_contract.plugins import PluginSpec

# The canonical spec filename — at a plugin repo's root and inside its wheel.
PLUGIN_SPEC_FILENAME = "tai-plugin.yml"

# Upper bound on spec content, applied to in-memory content, on-disk files, and
# the bytes inflated from a wheel member. A real ``tai-plugin.yml`` is a few KiB;
# 256 KiB is generous headroom while still refusing to slurp or decompress a
# multi-megabyte payload from an untrusted source.
MAX_PLUGIN_SPEC_BYTES = 256 * 1024


class PluginSpecLoadError(Exception):
    """Raised when a ``tai-plugin.yml`` cannot be located, read, or parsed.

    Covers the I/O and structural-safety failure family only; a well-formed
    YAML mapping that violates the schema raises pydantic's ``ValidationError``
    instead.
    """


class _SpecRejection(yaml.YAMLError):
    """A strict-loader rejection of a structurally dangerous YAML construct.

    A ``yaml.YAMLError`` subclass so it flows out of ``yaml.load`` like any
    parse failure; :func:`parse_plugin_spec` catches it ahead of generic YAML
    errors to attach the source and preserve its specific message.
    """


class _StrictSpecLoader(yaml.SafeLoader):
    """A ``SafeLoader`` that also rejects anchors, aliases, and duplicate keys.

    These are constructs a flat, trusted ``tai-plugin.yml`` never uses but which
    untrusted input weaponizes: anchors/aliases drive billion-laughs expansion,
    and a repeated mapping key silently shadows an earlier value (last wins).
    Both are refused with a clear message instead of being honored.
    """

    def fetch_anchor(self) -> None:
        raise _SpecRejection("YAML anchors/aliases are not permitted in tai-plugin.yml")

    def fetch_alias(self) -> None:
        raise _SpecRejection("YAML anchors/aliases are not permitted in tai-plugin.yml")

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        mapping = super().construct_mapping(node, deep=deep)
        # ``super`` collapses repeated keys last-wins; a shorter result than the
        # node's pair count means at least one key was duplicated. Reconstruct
        # keys only on that error path to name the offender.
        if len(mapping) != len(node.value):
            keys = [self.construct_object(key_node, deep=deep) for key_node, _ in node.value]
            duplicates = sorted({repr(key) for key in keys if keys.count(key) > 1})
            raise _SpecRejection(f"duplicate mapping key(s) not permitted in tai-plugin.yml: {', '.join(duplicates)}")
        return mapping


def parse_plugin_spec(data: bytes | str, *, source: str = PLUGIN_SPEC_FILENAME) -> PluginSpec:
    """Parse and validate ``tai-plugin.yml`` content already in memory.

    The shared low-level entry point: :func:`load_plugin_spec` and
    :func:`read_wheel_plugin_spec` both route through it, and callers holding
    raw bytes (e.g. an artifact fetched over the network) validate through it
    directly. ``source`` names the content's origin in error messages. Raises
    :class:`PluginSpecLoadError` for over-size input, non-UTF-8 bytes, invalid
    YAML, anchors/aliases, duplicate keys, excessive nesting, or a non-mapping
    document; pydantic ``ValidationError`` for schema violations.
    """
    if isinstance(data, bytes):
        if len(data) > MAX_PLUGIN_SPEC_BYTES:
            raise PluginSpecLoadError(
                f"{source} is {len(data)} bytes, exceeding the {MAX_PLUGIN_SPEC_BYTES}-byte limit"
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PluginSpecLoadError(f"{source} is not valid UTF-8: {exc}") from exc
    else:
        text = data
        encoded_size = len(text.encode("utf-8"))
        if encoded_size > MAX_PLUGIN_SPEC_BYTES:
            raise PluginSpecLoadError(
                f"{source} is {encoded_size} bytes, exceeding the {MAX_PLUGIN_SPEC_BYTES}-byte limit"
            )
    try:
        raw = yaml.load(text, Loader=_StrictSpecLoader)
    except _SpecRejection as exc:
        raise PluginSpecLoadError(f"{source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise PluginSpecLoadError(f"{source} is not valid YAML: {exc}") from exc
    except RecursionError as exc:
        raise PluginSpecLoadError(f"{source}: document nested too deeply") from exc
    if not isinstance(raw, dict):
        raise PluginSpecLoadError(f"{source}: expected a YAML mapping at the top level, got {type(raw).__name__}")
    return PluginSpec.model_validate(raw)


def load_plugin_spec(path: Path) -> PluginSpec:
    """Load and validate the ``tai-plugin.yml`` at ``path``.

    Raises :class:`PluginSpecLoadError` for an unreadable or over-size file,
    non-UTF-8 bytes, invalid YAML, or a non-mapping document; pydantic
    ``ValidationError`` for schema violations. Never returns a partial or
    defaulted spec.

    The on-disk size is checked against :data:`MAX_PLUGIN_SPEC_BYTES` via
    ``stat`` *before* any bytes are read, so a multi-gigabyte file is refused
    without being slurped into memory. The bytes are then handed to
    :func:`parse_plugin_spec`, which enforces the byte cap and UTF-8 decode
    uniformly with the wheel and in-memory paths — so a non-UTF-8 file raises
    :class:`PluginSpecLoadError` rather than escaping as ``UnicodeDecodeError``.
    """
    try:
        size = path.stat().st_size
        if size > MAX_PLUGIN_SPEC_BYTES:
            raise PluginSpecLoadError(
                f"plugin spec {path} is {size} bytes, exceeding the {MAX_PLUGIN_SPEC_BYTES}-byte limit"
            )
        data = path.read_bytes()
    except OSError as exc:
        raise PluginSpecLoadError(f"cannot read plugin spec {path}: {exc}") from exc
    return parse_plugin_spec(data, source=str(path))


def read_wheel_plugin_spec(wheel_path: Path) -> PluginSpec:
    """Read and validate the spec packaged inside the wheel at ``wheel_path``.

    The wheel must contain exactly one ``tai-plugin.yml`` entry (shipped as
    package-data in the plugin's import package); zero or several entries, an
    unreadable archive, a member using a compression method other than
    stored/deflate, an over-size or decompression-bomb member, invalid YAML, or
    a non-mapping document raise :class:`PluginSpecLoadError`, and schema
    violations raise pydantic's ``ValidationError``.

    Only ``ZIP_STORED`` and ``ZIP_DEFLATED`` members are read: the bounded read
    that caps a decompression bomb relies on ``ZipExtFile`` threading the length
    cap into the decompressor, which CPython does only on the deflate path, so a
    ``ZIP_BZIP2``/``ZIP_LZMA`` member would inflate its whole block unbounded.
    The compression method is checked before any byte is read.
    """
    try:
        with zipfile.ZipFile(wheel_path) as wheel:
            entries = [n for n in wheel.namelist() if n.rsplit("/", 1)[-1] == PLUGIN_SPEC_FILENAME]
            if not entries:
                raise PluginSpecLoadError(f"{wheel_path} contains no {PLUGIN_SPEC_FILENAME}")
            if len(entries) > 1:
                raise PluginSpecLoadError(
                    f"{wheel_path} contains several {PLUGIN_SPEC_FILENAME} entries: {sorted(entries)}"
                )
            # Reject any compression method other than stored/deflate BEFORE
            # reading a byte. The bounded read below caps inflation at MAX + 1
            # bytes only for ZIP_STORED and ZIP_DEFLATED: CPython's ZipExtFile
            # threads the length cap into the decompressor solely on the deflate
            # path, so a ZIP_BZIP2 or ZIP_LZMA member has its whole block
            # inflated on the first read() with no ceiling — a decompression
            # bomb the length check sees only after it has already exploded RSS.
            # compress_type is attacker-controlled central-directory metadata;
            # legitimate Python wheels only ever store or deflate their members,
            # so a bzip2/lzma member is anomalous and refused outright.
            entry = wheel.getinfo(entries[0])
            if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise PluginSpecLoadError(
                    f"{wheel_path}:{entries[0]} uses unsupported compression "
                    f"(method {entry.compress_type}); only stored/deflate are allowed"
                )
            # Bounded-decompression read: cap the output at MAX + 1 bytes so a
            # decompression bomb never fully expands. ZipInfo.file_size cannot be
            # trusted here — it is attacker-controlled central-directory metadata,
            # and a bare wheel.read() decompresses the entire deflate stream into
            # memory before truncating to that (possibly lying) size. Reading a
            # bounded count through the stream instead makes ZipExtFile stop
            # inflating once ~MAX + 1 bytes are produced, so an over-cap member is
            # rejected on real inflated bytes, not a declared size.
            with wheel.open(entry) as member:
                data = member.read(MAX_PLUGIN_SPEC_BYTES + 1)
            if len(data) > MAX_PLUGIN_SPEC_BYTES:
                raise PluginSpecLoadError(
                    f"{wheel_path}:{entries[0]} inflates past the {MAX_PLUGIN_SPEC_BYTES}-byte limit"
                )
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, zlib.error) as exc:
        # BadZipFile/OSError: corrupt or unreadable archive; RuntimeError: an
        # encrypted member; NotImplementedError: an unsupported compression
        # method; zlib.error: mid-stream deflate corruption raised during the
        # member read (zlib.error does not subclass OSError, so it must be named
        # explicitly). All mean "cannot read this wheel", per the docstring.
        raise PluginSpecLoadError(f"cannot read wheel {wheel_path}: {exc}") from exc
    return parse_plugin_spec(data, source=f"{wheel_path}:{entries[0]}")


__all__ = [
    "MAX_PLUGIN_SPEC_BYTES",
    "PLUGIN_SPEC_FILENAME",
    "PluginSpecLoadError",
    "load_plugin_spec",
    "parse_plugin_spec",
    "read_wheel_plugin_spec",
]
