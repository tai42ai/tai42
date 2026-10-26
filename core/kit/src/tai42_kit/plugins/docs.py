"""Plugin docs: reading and validating the ``docs/`` tree beside ``tai-plugin.yml``.

A plugin ships a ``docs/`` directory beside its packaged spec, whose landing page
is ``docs/index.mdx``. :func:`read_wheel_docs` extracts that tree from a wheel and
:func:`read_dir_docs` reads it from a plugin directory, both under bounded reads
with per-file and cumulative ceilings; :func:`validate_docs` is the canonical
validation contract every consumer applies to the extracted tree (set shape,
front matter, references, and the safe-mdx subset for third-party docs). Every
failure raises :class:`PluginDocsError`.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
import zlib
from pathlib import Path

import yaml

from tai42_kit.plugins.spec import PLUGIN_SPEC_FILENAME, _SpecRejection, _StrictSpecLoader

# The in-wheel docs tree: a ``docs/`` directory beside the packaged
# ``tai-plugin.yml``, whose landing page is ``docs/index.mdx``. Every consumer
# (marketplace ingest, the monorepo CI gate, the docs-site generator) names
# these from here rather than re-spelling them.
PLUGIN_DOCS_DIRNAME = "docs"
PLUGIN_DOCS_INDEX = "docs/index.mdx"

# Per-file and cumulative ceilings on an extracted docs tree, enforced by
# :func:`read_wheel_docs` via bounded reads on untrusted (possibly bomb) wheel
# members. Kit is the SOLE owner: every consumer imports these, none re-declares.
MAX_DOCS_FILE_BYTES = 2 * 1024 * 1024
MAX_DOCS_TOTAL_BYTES = 16 * 1024 * 1024


class PluginDocsError(Exception):
    """Raised when a plugin's in-wheel ``docs/`` tree cannot be read or fails the canonical validation contract.

    Covers both the extraction family (missing tree, a traversing/absolute
    member, an over-ceiling or bomb member, an unreadable archive) and every
    :func:`validate_docs` violation (set-shape, front matter, unresolvable
    reference, forbidden scheme, un-whitelisted mdx). Nothing degrades silently.
    """


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


def _resolve_docs_root(plugin_dir: Path) -> Path:
    """The resolved real ``docs/`` root of ``plugin_dir``, or a :class:`PluginDocsError`.

    Plugin-provided paths are an untrusted boundary and a symlinked member
    resolves/reads through to its target, so containment is anchored on the
    plugin dir: ``docs/`` must be a real directory inside it (a symlinked root
    would resolve to an external tree whose files then look contained). A missing
    or symlinked/escaping root raises.
    """
    docs_root = plugin_dir / PLUGIN_DOCS_DIRNAME
    if not docs_root.is_dir():
        raise PluginDocsError(f"{plugin_dir} has no {PLUGIN_DOCS_DIRNAME}/ directory at {docs_root}")
    plugin_dir_real = plugin_dir.resolve()
    docs_root_real = docs_root.resolve()
    if docs_root.is_symlink() or not docs_root_real.is_relative_to(plugin_dir_real):
        raise PluginDocsError(f"{docs_root} is not a real {PLUGIN_DOCS_DIRNAME}/ directory inside {plugin_dir_real}")
    return docs_root_real


def _read_capped_docs_member(path: Path, docs_root_real: Path) -> bytes:
    """Return one docs member's bytes, enforcing containment and the per-file size cap.

    Recheck the path resolves inside ``docs_root_real``, refuse an over-cap file by ``stat`` before
    reading, then bounded-read at the per-file ceiling. Any escape, unreadable file, or over-cap file
    raises :class:`PluginDocsError`.
    """
    if not path.resolve().is_relative_to(docs_root_real):
        raise PluginDocsError(f"{path} resolves outside the {PLUGIN_DOCS_DIRNAME}/ tree at {docs_root_real}")
    # ``stat`` first refuses an over-cap file before any bytes are read; the
    # bounded read then caps at MAX + 1 so a file that grew after the stat is
    # still refused by the length check below rather than slurped whole.
    try:
        size = path.stat().st_size
        if size > MAX_DOCS_FILE_BYTES:
            raise PluginDocsError(f"{path} is {size} bytes, past the {MAX_DOCS_FILE_BYTES}-byte per-file limit")
        with path.open("rb") as fh:
            data = fh.read(MAX_DOCS_FILE_BYTES + 1)
    except OSError as exc:
        raise PluginDocsError(f"cannot read docs member {path}: {exc}") from exc
    if len(data) > MAX_DOCS_FILE_BYTES:
        raise PluginDocsError(f"{path} is past the {MAX_DOCS_FILE_BYTES}-byte per-file limit")
    return data


def read_dir_docs(plugin_dir: Path) -> dict[str, bytes]:
    """Read the ``docs/`` tree sitting beside ``tai-plugin.yml`` in a plugin dir.

    The on-disk counterpart of :func:`read_wheel_docs` for a descriptor-only
    plugin (no wheel): returns ``{relative_path: bytes}`` for every file under
    ``<plugin_dir>/docs/``, keyed from ``plugin_dir`` so keys begin ``docs/``
    (e.g. ``docs/index.mdx``) exactly as the wheel reader keys them. The same
    ceilings apply: a member past :data:`MAX_DOCS_FILE_BYTES` or a cumulative
    payload past :data:`MAX_DOCS_TOTAL_BYTES` raises :class:`PluginDocsError`. A
    missing ``docs/`` directory, an empty one, or an unreadable file also raises
    :class:`PluginDocsError`. Never returns a partial tree.
    """
    docs_root_real = _resolve_docs_root(plugin_dir)
    docs_root = plugin_dir / PLUGIN_DOCS_DIRNAME
    try:
        members = sorted(path for path in docs_root.rglob("*") if path.is_file())
    except OSError as exc:
        raise PluginDocsError(f"cannot read docs tree at {docs_root}: {exc}") from exc
    if not members:
        raise PluginDocsError(f"{plugin_dir} packages no {PLUGIN_DOCS_DIRNAME}/ tree at {docs_root}")
    files: dict[str, bytes] = {}
    total = 0
    for path in members:
        data = _read_capped_docs_member(path, docs_root_real)
        total += len(data)
        if total > MAX_DOCS_TOTAL_BYTES:
            raise PluginDocsError(f"{docs_root} docs tree is past the {MAX_DOCS_TOTAL_BYTES}-byte total limit")
        files[path.relative_to(plugin_dir).as_posix()] = data
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
    """Validate a docs tree (``{docs-relative-path: bytes}``) against the canonical contract.

    Raise :class:`PluginDocsError` on the first violation.
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
        meta = yaml.load(match.group(1), Loader=_StrictSpecLoader)  # noqa: S506 loader subclasses yaml.SafeLoader; no arbitrary object construction
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
    """Blank front matter, fenced code blocks (```` ``` ````/``~~~``), and inline ``code`` spans.

    Line count and within-line offsets are preserved, so the reference and mdx scans see only real
    body text (a link or ``<tag>`` shown inside a code sample is documentation, not live). It masks only
    these, and only over-scans, never hiding live content: a 4-space-indented code block is NOT masked, so an
    indented sample carrying braces/tags/link-like text is conservatively rejected
    for third-party docs — the fail-closed choice (authors use fenced blocks).
    """
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
    """The in-set key a scheme-less reference resolves to, or None for a pure in-page anchor/query.

    A pure anchor/query resolves to the page itself.
    """
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
