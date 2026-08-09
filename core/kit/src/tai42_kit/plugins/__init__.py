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

import posixpath
import re
import zipfile
import zlib
from pathlib import Path
from typing import Any

import yaml
from tai42_contract.plugins import PluginSpec

# The canonical spec filename — at a plugin repo's root and inside its wheel.
PLUGIN_SPEC_FILENAME = "tai-plugin.yml"

# The in-wheel docs tree: a ``docs/`` directory beside the packaged
# ``tai-plugin.yml``, whose landing page is ``docs/index.mdx``. Every consumer
# (marketplace ingest, the monorepo CI gate, the docs-site generator) names
# these from here rather than re-spelling them.
PLUGIN_DOCS_DIRNAME = "docs"
PLUGIN_DOCS_INDEX = "docs/index.mdx"

# Upper bound on spec content, applied to in-memory content, on-disk files, and
# the bytes inflated from a wheel member. A real ``tai-plugin.yml`` is a few KiB;
# 256 KiB is generous headroom while still refusing to slurp or decompress a
# multi-megabyte payload from an untrusted source.
MAX_PLUGIN_SPEC_BYTES = 256 * 1024

# Per-file and cumulative ceilings on an extracted docs tree, enforced by
# :func:`read_wheel_docs` via bounded reads on untrusted (possibly bomb) wheel
# members. Kit is the SOLE owner: every consumer imports these, none re-declares.
MAX_DOCS_FILE_BYTES = 2 * 1024 * 1024
MAX_DOCS_TOTAL_BYTES = 16 * 1024 * 1024


class PluginSpecLoadError(Exception):
    """Raised when a ``tai-plugin.yml`` cannot be located, read, or parsed.

    Covers the I/O and structural-safety failure family only; a well-formed
    YAML mapping that violates the schema raises pydantic's ``ValidationError``
    instead.
    """


class PluginDocsError(Exception):
    """Raised when a plugin's in-wheel ``docs/`` tree cannot be read or fails
    the canonical validation contract.

    Covers both the extraction family (missing tree, a traversing/absolute
    member, an over-ceiling or bomb member, an unreadable archive) and every
    :func:`validate_docs` violation (set-shape, front matter, unresolvable
    reference, forbidden scheme, un-whitelisted mdx). Nothing degrades silently.
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


def read_wheel_docs(wheel_path: Path) -> dict[str, bytes]:
    """Read the ``docs/`` tree packaged beside ``tai-plugin.yml`` in the wheel.

    Returns ``{relative_path: bytes}`` for every file under the packaged
    ``docs/`` directory, keyed from the spec's package root so keys begin
    ``docs/`` (e.g. ``docs/index.mdx``). The wheel must contain exactly one
    ``tai-plugin.yml`` (as :func:`read_wheel_plugin_spec` requires) with a
    ``docs/`` directory beside it. Zero or several specs, a missing ``docs/``
    tree, a member escaping via ``..`` or an absolute path, a member using a
    compression method other than stored/deflate, a member inflating past
    :data:`MAX_DOCS_FILE_BYTES`, a cumulative payload past
    :data:`MAX_DOCS_TOTAL_BYTES`, or an unreadable archive raise
    :class:`PluginDocsError`. Never returns a partial tree.

    Decompression-bomb discipline mirrors :func:`read_wheel_plugin_spec`: only
    ``ZIP_STORED``/``ZIP_DEFLATED`` members are read (CPython threads the length
    cap into the decompressor solely on the deflate path), the caps are enforced
    by BOUNDED streamed reads, and ``ZipInfo.file_size`` — attacker-controlled
    central-directory metadata — is never trusted.
    """
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(wheel_path) as wheel:
            names = wheel.namelist()
            specs = [n for n in names if n.rsplit("/", 1)[-1] == PLUGIN_SPEC_FILENAME]
            if not specs:
                raise PluginDocsError(f"{wheel_path} contains no {PLUGIN_SPEC_FILENAME}")
            if len(specs) > 1:
                raise PluginDocsError(f"{wheel_path} contains several {PLUGIN_SPEC_FILENAME} entries: {sorted(specs)}")
            # docs/ is the sibling of the in-package spec: strip the spec's
            # basename to get its package root, then read under ``<root>docs/``.
            package_root = specs[0][: -len(PLUGIN_SPEC_FILENAME)]
            docs_root = f"{package_root}{PLUGIN_DOCS_DIRNAME}/"
            members = [n for n in names if n.startswith(docs_root) and not n.endswith("/")]
            if not members:
                raise PluginDocsError(f"{wheel_path} packages no {PLUGIN_DOCS_DIRNAME}/ tree at {docs_root}")
            total = 0
            for name in members:
                relative = name[len(package_root) :]
                # A member escaping its tree via ``..`` or an absolute path could
                # write outside the docs root when a naive consumer extracts it.
                # Split on BOTH separators: a backslash-separated ``..`` is one
                # forward-slash segment, so a ``/``-only split would miss it.
                if name.startswith(("/", "\\")) or ".." in re.split(r"[\\/]", relative):
                    raise PluginDocsError(f"{wheel_path}:{name} is an unsafe (traversing or absolute) member path")
                info = wheel.getinfo(name)
                # Reject any compression method other than stored/deflate before
                # reading a byte — the bounded read below caps inflation only on
                # those two methods (see read_wheel_plugin_spec).
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise PluginDocsError(
                        f"{wheel_path}:{name} uses unsupported compression "
                        f"(method {info.compress_type}); only stored/deflate are allowed"
                    )
                # Bounded-decompression read: cap output at MAX + 1 inflated bytes
                # so a bomb never fully expands; ZipInfo.file_size is untrusted.
                with wheel.open(info) as member:
                    data = member.read(MAX_DOCS_FILE_BYTES + 1)
                if len(data) > MAX_DOCS_FILE_BYTES:
                    raise PluginDocsError(
                        f"{wheel_path}:{name} inflates past the {MAX_DOCS_FILE_BYTES}-byte per-file limit"
                    )
                total += len(data)
                if total > MAX_DOCS_TOTAL_BYTES:
                    raise PluginDocsError(
                        f"{wheel_path} docs tree inflates past the {MAX_DOCS_TOTAL_BYTES}-byte total limit"
                    )
                files[relative] = data
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, zlib.error) as exc:
        # Same unreadable-archive family read_wheel_plugin_spec maps: corrupt or
        # unreadable zip, encrypted member, unsupported method, or mid-stream
        # deflate corruption (zlib.error does not subclass OSError).
        raise PluginDocsError(f"cannot read wheel {wheel_path}: {exc}") from exc
    return files


# --- Canonical docs validation ------------------------------------------------

_DOCS_PREFIX = f"{PLUGIN_DOCS_DIRNAME}/"
_DOCS_IMAGES_PREFIX = f"{PLUGIN_DOCS_DIRNAME}/images/"
_MDX_SUFFIX = ".mdx"

# Raster image extensions permitted under ``docs/images/``. SVG is excluded
# deliberately — an SVG is an active-content document (a scriptable XSS surface),
# not an inert raster — so it is refused for EVERY namespace at this earliest gate.
_RASTER_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

# The safe-mdx whitelist enforced on THIRD-PARTY docs only. It starts empty — the
# plain-markdown subset admits no raw HTML/JSX tag and no ESM import/export, each
# a script-injection surface once the mdx is rendered. First-party docs (trusted
# authors) are exempt. Widen only deliberately, and only with tags carrying no
# active-content attribute surface.
MDX_SAFE_COMPONENTS: frozenset[str] = frozenset()

# Front-matter title/description are re-emitted verbatim into generated pages'
# ``---`` YAML fences downstream, so a value may carry none of these: quotes and
# backslashes (which break a quoted re-emission) or the ``---`` fence marker.
# Control characters are rejected separately — a newline could open a new key or
# a fence line, injecting unvalidated body past the third-party whitelist.
_FRONT_MATTER_FORBIDDEN_CHARS = frozenset("\"'\\")

# Leading YAML front-matter block: a ``---`` fence, the mapping, a closing ``---``.
_FRONT_MATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
# Inline markdown link/image target: ``[text](target)`` / ``![alt](target)`` —
# group 1 is the target, up to the first whitespace (a following "title") or ``)``.
_LINK_RE = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)")
# A markdown reference-link/image DEFINITION at (masked) line start:
# ``[id]: destination "title"``. group 1 is the destination — a leading ``<`` is
# consumed outside the group and a wrapping ``>`` is excluded from it, so a
# ``<dest>``-wrapped destination is captured unwrapped. These destinations bypass
# the inline scan, so they get the SAME scheme/in-set check. The ``(?!\^)`` skips
# GFM footnote definitions (``[^label]: text``), which carry a body, not a URL
# destination — matching one would flag its first body word as an unresolved ref.
_REF_DEF_RE = re.compile(r"(?m)^[ \t]{0,3}\[(?!\^)[^\]]+\]:[ \t]*<?([^\s>]+)")
# A URL scheme prefix (``http:``, ``javascript:``, ``data:``, …).
_SCHEME_RE = re.compile(r"\A[A-Za-z][A-Za-z0-9+.\-]*:")
# A raw HTML/JSX tag open — no space between ``<`` and the tag name, so prose
# ``a < b`` is not a tag. Autolinks are stripped before this is applied.
_HTML_TAG_RE = re.compile(r"</?([A-Za-z][A-Za-z0-9.\-]*)")
# A markdown autolink (``<https://…>`` / ``<user@host>``): a ``<…>`` that is a URL
# or email, NOT a tag — excluded from the tag scan.
_AUTOLINK_RE = re.compile(r"<[A-Za-z][A-Za-z0-9+.\-]*://[^>\s]*>|<[^>\s@]+@[^>\s]+>")
# MDX ESM module syntax at line start (real ``import … from`` / ``export …``
# forms, not prose beginning with the word "import").
_ESM_RE = re.compile(
    r"(?m)^[ \t]*(?:import\s+[^\n]*\bfrom\b|import\s+['\"]|export\s+(?:default|const|let|var|function|class|\{|\*))"
)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
# An MDX expression container: a ``{`` opens ``{ jsExpr }`` — arbitrary evaluated
# JavaScript, a code-execution primitive absent from plain markdown. Escaping is by
# backslash parity: a ``{`` preceded by an EVEN run of backslashes (zero included)
# is live and rejected; an ODD run (``\{``, ``\\\{``) escapes it to a literal brace
# and is allowed. ``(?:\\\\)*`` consumes the even pairs, ``(?<!\\)`` anchors the run
# to a non-backslash so ``\\{`` (escaped backslash, then a live ``{``) still matches.
_MDX_EXPR_RE = re.compile(r"(?<!\\)(?:\\\\)*\{")


def validate_docs(files: dict[str, bytes], *, first_party: bool) -> None:
    """Validate a docs tree (``{docs-relative-path: bytes}``) against the
    canonical contract, raising :class:`PluginDocsError` on the first violation.

    The one implementation every enforcement point shares (marketplace ingest,
    the monorepo CI gate, the docs-site generator); each caller owns the
    ``first_party`` policy. Enforced for EVERY namespace: the set shape (only
    ``.mdx`` pages under ``docs/`` and raster images under ``docs/images/`` — SVG
    and every other type refused, SVG being an XSS surface); a present
    ``docs/index.mdx``; per-page front matter of exactly ``title`` +
    ``description``, each a single-line plain string with no control characters
    or fence-breaking metacharacters; and relative link/image references that
    resolve within the set. When ``first_party`` is False, additionally: the mdx
    safe-whitelist (:data:`MDX_SAFE_COMPONENTS`) and a link/image scheme of only
    http(s) or an in-set relative path — ``javascript:``/``data:`` refused.
    """
    if PLUGIN_DOCS_INDEX not in files:
        raise PluginDocsError(f"required docs page {PLUGIN_DOCS_INDEX} is missing")
    mdx_pages: list[str] = []
    for path in sorted(files):
        if not path.startswith(_DOCS_PREFIX):
            raise PluginDocsError(f"{path}: docs member lies outside the {_DOCS_PREFIX} tree")
        suffix = posixpath.splitext(path)[1].lower()
        if path.startswith(_DOCS_IMAGES_PREFIX):
            if suffix not in _RASTER_IMAGE_SUFFIXES:
                raise PluginDocsError(
                    f"{path}: only raster images ({', '.join(sorted(_RASTER_IMAGE_SUFFIXES))}) are allowed "
                    f"under {_DOCS_IMAGES_PREFIX} (got {suffix or 'no extension'})"
                )
        elif suffix == _MDX_SUFFIX:
            mdx_pages.append(path)
        else:
            raise PluginDocsError(
                f"{path}: only .mdx pages under {_DOCS_PREFIX} and raster images under "
                f"{_DOCS_IMAGES_PREFIX} are permitted (got {suffix or 'no extension'})"
            )
    for page in mdx_pages:
        _validate_page(page, files[page], files, first_party=first_party)


def _validate_page(page: str, data: bytes, files: dict[str, bytes], *, first_party: bool) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PluginDocsError(f"{page} is not valid UTF-8: {exc}") from exc
    _validate_front_matter(page, text)
    masked = _mask_front_matter_and_code(text)
    _validate_references(page, masked, files, first_party=first_party)
    if not first_party:
        _reject_unsafe_mdx(page, masked)


def _validate_front_matter(page: str, text: str) -> None:
    match = _FRONT_MATTER_RE.match(text)
    if match is None:
        raise PluginDocsError(f"{page}: missing a leading `---` front-matter block with title and description")
    try:
        meta = yaml.load(match.group(1), Loader=_StrictSpecLoader)
    except _SpecRejection:
        raise PluginDocsError(f"{page}: front matter must not use YAML anchors, aliases, or duplicate keys") from None
    except yaml.YAMLError as exc:
        raise PluginDocsError(f"{page}: front matter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise PluginDocsError(f"{page}: front matter must be a mapping of title and description")
    if set(meta) != {"title", "description"}:
        raise PluginDocsError(f"{page}: front matter must be exactly `title` and `description`, got {sorted(meta)}")
    for key in ("title", "description"):
        _check_front_matter_value(page, key, meta[key])


def _check_front_matter_value(page: str, key: str, value: object) -> None:
    if not isinstance(value, str):
        raise PluginDocsError(f"{page}: front-matter `{key}` must be a plain string, got {type(value).__name__}")
    if value == "":
        raise PluginDocsError(f"{page}: front-matter `{key}` must not be empty")
    if any(ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in value):
        raise PluginDocsError(f"{page}: front-matter `{key}` must be a single line with no control characters")
    if _FRONT_MATTER_FORBIDDEN_CHARS.intersection(value) or "---" in value:
        raise PluginDocsError(
            f"{page}: front-matter `{key}` must not contain quotes, backslashes, or `---` "
            "(re-emitted into generated front matter — fence-injection risk)"
        )


def _mask_front_matter_and_code(text: str) -> str:
    """Blank the front matter, fenced code blocks (```` ``` ````/``~~~``), and inline
    ``code`` spans, preserving line count and within-line offsets, so the reference
    and mdx scans see only real body text (a link or ``<tag>`` shown inside a code
    sample is documentation, not live). It masks only these, and only over-scans,
    never hiding live content: a 4-space-indented code block is NOT masked, so an
    indented sample carrying braces/tags/link-like text is conservatively rejected
    for third-party docs — the fail-closed choice (authors use fenced blocks)."""
    out: list[str] = []
    in_front_matter = False
    fence: str | None = None
    for index, line in enumerate(text.split("\n")):
        stripped = line.strip()
        if index == 0 and stripped == "---":
            in_front_matter = True
            out.append("")
            continue
        if in_front_matter:
            out.append("")
            if stripped == "---":
                in_front_matter = False
            continue
        lead = line.lstrip()
        if fence is None and lead.startswith(("```", "~~~")):
            fence = lead[:3]
            out.append("")
            continue
        if fence is not None:
            closing = lead.startswith(fence)
            out.append("")
            if closing:
                fence = None
            continue
        out.append(line)
    return _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), "\n".join(out))


def _validate_references(page: str, masked: str, files: dict[str, bytes], *, first_party: bool) -> None:
    # Inline ``](target)`` and reference DEFINITIONS ``[id]: dest`` both carry a
    # destination; each destination runs the identical scheme/in-set check, so a
    # ``[x]: javascript:…`` or off-set definition cannot slip past the inline scan.
    for pattern in (_LINK_RE, _REF_DEF_RE):
        for match in pattern.finditer(masked):
            line = masked.count("\n", 0, match.start(1)) + 1
            _check_reference(page, match.group(1), line, files, first_party=first_party)


def _check_reference(page: str, raw: str, line: int, files: dict[str, bytes], *, first_party: bool) -> None:
    target = raw.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    if target == "":
        return
    scheme = _SCHEME_RE.match(target)
    if scheme is not None:
        if not first_party and scheme.group(0)[:-1].lower() not in ("http", "https"):
            raise PluginDocsError(
                f"{page}:{line}: link/image scheme {scheme.group(0)!r} is not permitted in third-party "
                "docs (only http(s) or an in-set relative reference)"
            )
        return
    # A single leading slash is a site-root route (``/concepts/…``); a ``//host``
    # protocol-relative target is not one and falls through to in-set resolution.
    if target.startswith("/") and not target.startswith("//"):
        if first_party:
            # A first-party site-root route resolves only against the unified docs
            # site, whose links are validated at docs-site build time, not this set.
            return
        raise PluginDocsError(
            f"{page}:{line}: site-root-relative reference {target!r} is not permitted in third-party "
            "docs (only http(s) or an in-set relative reference)"
        )
    resolved = _resolve_reference(page, target)
    if resolved is None:
        return
    if resolved not in files:
        raise PluginDocsError(
            f"{page}:{line}: relative reference {target!r} resolves to {resolved!r}, not in the docs set"
        )


def _resolve_reference(page: str, target: str) -> str | None:
    """The in-set key a scheme-less reference resolves to, or None for a pure
    in-page anchor/query (which resolves to the page itself)."""
    path_part = target.split("#", 1)[0].split("?", 1)[0]
    if path_part == "":
        return None
    return posixpath.normpath(posixpath.join(posixpath.dirname(page), path_part))


def _reject_unsafe_mdx(page: str, masked: str) -> None:
    esm = _ESM_RE.search(masked)
    if esm is not None:
        line = masked.count("\n", 0, esm.start()) + 1
        raise PluginDocsError(f"{page}:{line}: MDX `import`/`export` is not permitted in third-party docs")
    expr = _MDX_EXPR_RE.search(masked)
    if expr is not None:
        line = masked.count("\n", 0, expr.start()) + 1
        raise PluginDocsError(
            f"{page}:{line}: raw MDX expressions (an unescaped `{{`) are not permitted in the "
            "plain-markdown third-party subset"
        )
    scannable = _AUTOLINK_RE.sub(lambda m: " " * len(m.group(0)), masked)
    for match in _HTML_TAG_RE.finditer(scannable):
        tag = match.group(1)
        if tag not in MDX_SAFE_COMPONENTS:
            line = scannable.count("\n", 0, match.start()) + 1
            raise PluginDocsError(
                f"{page}:{line}: raw HTML/JSX tag <{tag}> is not permitted in third-party docs "
                "(plain-markdown subset only)"
            )


__all__ = [
    "MAX_DOCS_FILE_BYTES",
    "MAX_DOCS_TOTAL_BYTES",
    "MAX_PLUGIN_SPEC_BYTES",
    "MDX_SAFE_COMPONENTS",
    "PLUGIN_DOCS_DIRNAME",
    "PLUGIN_DOCS_INDEX",
    "PLUGIN_SPEC_FILENAME",
    "PluginDocsError",
    "PluginSpecLoadError",
    "load_plugin_spec",
    "parse_plugin_spec",
    "read_wheel_docs",
    "read_wheel_plugin_spec",
    "validate_docs",
]
