"""Storage backend for deep agents.

Serves two needs through one interface:

* **Skills** — read-only skill BUNDLES, read live from the app's template provider
  (``tai42_app.storage.resource_manager``) and managed through the template tools
  (``upload_template`` / ``list_resources``). A skill is a directory
  ``skills/<name>/`` holding a ``SKILL.md`` plus any bundled reference files and
  scripts; the backend serves the WHOLE tree (per the Agent Skills spec, which lets
  a bundle reference files and scripts), so ``ls``/``read``/``glob``/``grep`` over
  the skills mount see every bundled file, not only ``SKILL.md``. Files are read
  as raw bytes: a binary file (by deepagents' read classification) is served as
  its base64 payload, a text file as UTF-8 text.
* **Scratch filesystem** — the files the agent writes while it works. These live in
  per-thread agent state so concurrent sessions never clobber each other.

:func:`build_backend` composes a :class:`StateBackend` default (scratch) with
skills routed to a read-only :class:`TemplateSkillsBackend` mounted at
:data:`SKILLS_ROOT`.
"""

from __future__ import annotations

import base64
from collections.abc import Iterable

import wcmatch.glob as wcglob
from deepagents.backends import BackendProtocol, CompositeBackend, StateBackend
from deepagents.backends.protocol import (
    EditResult,
    FileData,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import (
    _get_backend_read_file_type,
    create_file_data,
    grep_matches_from_files,
    slice_read_response,
)
from tai42_contract.app import tai42_app

#: Mount point for skills inside the agent's virtual filesystem (e.g.
#: ``"/skills/jq/"``); :func:`build_backend` routes this prefix to the template backend.
SKILLS_ROOT = "/skills/"

#: Prefix under which skill artifacts live in the template provider; a skill named
#: ``jq`` is the template key ``skills/jq/SKILL.md``. The ``upload_template`` tool
#: and this backend both use this convention.
_SKILLS_KEY_PREFIX = "skills/"

#: File every skill directory must contain, per the Agent Skills specification.
_SKILL_FILENAME = "SKILL.md"


def _backend_path_to_key(backend_path: str) -> str:
    """Map a deepagents backend path to a template-provider key.

    ``CompositeBackend`` strips the :data:`SKILLS_ROOT` route prefix before
    delegating, so this backend receives a path rooted at the skills mount with
    a leading slash (e.g. ``"/jq/SKILL.md"``). The template key is that path
    without the leading slash, under :data:`_SKILLS_KEY_PREFIX`
    (``"skills/jq/SKILL.md"``).
    """
    return f"{_SKILLS_KEY_PREFIX}{backend_path.lstrip('/')}"


def _key_to_backend_path(key: str) -> str:
    """Map a template-provider key back to a deepagents backend path.

    Inverse of :func:`_backend_path_to_key`: ``"skills/jq/SKILL.md"`` ->
    ``"/jq/SKILL.md"``.
    """
    return "/" + key[len(_SKILLS_KEY_PREFIX) :]


async def _load_skill_bytes(key: str) -> bytes:
    """Return the raw bytes of a listed skill key.

    Read through the resource manager's byte loader, which root-contains the key;
    a key that vanished after the listing raises the storage's own error.
    """
    data, _mime = await tai42_app.storage.resource_manager.load(key, with_mime=False)
    return data


def _is_binary_path(backend_path: str) -> bool:
    """Whether deepagents reads ``backend_path`` as binary (the ``read_file`` tool's own routing)."""
    return _get_backend_read_file_type(backend_path) != "text"


async def _text_file_index(keys: Iterable[str]) -> dict[str, FileData]:
    """Return the text view of ``keys`` for search, keyed by backend path.

    A binary file (by suffix or by bytes that are not UTF-8) is not searched,
    matching ripgrep's and deepagents' filesystem grep's binary-file skip, so
    every hit is a line ``read_file`` shows as text.
    """
    files: dict[str, FileData] = {}
    for key in keys:
        backend_path = _key_to_backend_path(key)
        if _is_binary_path(backend_path):
            continue
        data = await _load_skill_bytes(key)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        files[backend_path] = create_file_data(text, encoding="utf-8")
    return files


def _glob_skill_paths(paths: Iterable[str], pattern: str, path: str) -> list[str]:
    """Return the backend paths matching a glob, over a fixed set of skill paths.

    Reproduces the deepagents filesystem glob:

    * ``path`` selects the search base, normalized to an absolute trailing-slash-free
      form (root stays ``"/"``); only paths at or below it are matched, relative to
      it. A whitespace-only ``path`` yields no matches.
    * A leading ``"/"`` on ``pattern`` is stripped, so ``"/jq/SKILL.md"`` and
      ``"jq/SKILL.md"`` match the same.
    * :func:`wcmatch.glob.globmatch` with ``BRACE | GLOBSTAR``: ``*`` does not cross
      ``/``, ``**`` is the recursive form, ``{a,b}`` expands.

    Matches are returned sorted ascending (every skill file is stamped identically,
    so ``modified_at`` ordering is immaterial).
    """
    base = path or "/"
    if not base.strip():
        return []
    normalized = base if base.startswith("/") else "/" + base
    if normalized != "/" and normalized.endswith("/"):
        normalized = normalized.rstrip("/")

    all_paths = list(paths)
    if normalized in all_paths:
        candidates = [normalized]
    elif normalized == "/":
        candidates = [p for p in all_paths if p.startswith("/")]
    else:
        dir_prefix = normalized + "/"
        candidates = [p for p in all_paths if p.startswith(dir_prefix)]

    effective_pattern = pattern.lstrip("/")
    matches: list[str] = []
    for file_path in candidates:
        if normalized == "/":
            relative = file_path[1:]
        elif file_path == normalized:
            relative = file_path.split("/")[-1]
        else:
            relative = file_path[len(normalized) + 1 :]
        if wcglob.globmatch(relative, effective_pattern, flags=wcglob.BRACE | wcglob.GLOBSTAR):
            matches.append(file_path)
    return sorted(matches)


class TemplateSkillsBackend(BackendProtocol):
    """Read-only deepagents backend backed by the app's template provider.

    Serves whole skill bundles live from ``tai42_app.storage.resource_manager``
    as raw bytes (``load``), never jinja-rendered: every key under
    ``skills/<name>/`` (``SKILL.md`` plus bundled reference files, scripts and
    assets) is listable, readable and globbable, and its text files are greppable. Every
    write/edit/upload path raises loudly; skills are managed through the
    ``upload_template`` / ``list_resources`` tools.
    """

    async def _list_skill_keys(self) -> list[str]:
        """Return every served skill-bundle key under ``skills/`` in the provider.

        Serving is not ``SKILL.md``-gated: a bundle's reference files and scripts
        are served under their real keys too (a file under a directory that lacks
        ``SKILL.md`` is still listed, never silently dropped). The narrower
        "which directories are skills" question stays ``SKILL.md``-gated and is
        answered where it is asked (:meth:`als`'s skill-directory self entry).
        """
        keys = await tai42_app.storage.resource_manager.list_resources()
        return [key for key in keys if key.startswith(_SKILLS_KEY_PREFIX) and len(key) > len(_SKILLS_KEY_PREFIX)]

    async def als(self, path: str) -> LsResult:
        """List the DIRECT children of ``path`` across the served skill bundles.

        ``path`` is rooted at the skills mount (e.g. ``"/"`` for the whole skills
        tree, ``"/jq/"`` for one skill, or ``"/jq/references/"`` for a subdir).
        Each direct child is emitted once: a file key as a file entry, and an
        intermediate directory (``references/``, ``scripts/``) as an ``is_dir``
        entry SYNTHESIZED from the flat key set (a directory exists iff some key
        extends ``path`` through it).

        In addition, when ``path`` is itself a skill directory (its ``SKILL.md``
        is served) the directory's OWN entry is emitted alongside its children.
        The skills middleware discovers a per-skill source ``/skills/<name>/`` by
        calling ``als`` on it and downloading ``<is_dir>/SKILL.md`` for each
        ``is_dir`` entry it returns; the self entry is what keeps that source
        discoverable, while extra bundle subdirs it also finds carry no
        ``SKILL.md`` and are harmlessly skipped (an expected ``file_not_found``).
        """
        normalized = path if path.endswith("/") else path + "/"
        key_set = set(await self._list_skill_keys())
        entries: dict[str, FileInfo] = {}
        for key in key_set:
            backend_path = _key_to_backend_path(key)
            if not backend_path.startswith(normalized):
                continue
            head, sep, _rest = backend_path[len(normalized) :].partition("/")
            if not head:
                continue
            if sep:
                dir_path = normalized + head + "/"
                entries[dir_path] = FileInfo(path=dir_path, is_dir=True, size=0, modified_at="")
            else:
                file_path = normalized + head
                entries[file_path] = FileInfo(path=file_path, is_dir=False, size=0, modified_at="")
        if normalized != "/" and _backend_path_to_key(normalized + _SKILL_FILENAME) in key_set:
            entries[normalized] = FileInfo(path=normalized, is_dir=True, size=0, modified_at="")
        ordered = sorted(entries.values(), key=lambda info: info["path"])
        return LsResult(entries=ordered)

    def ls(self, path: str) -> LsResult:
        """Async-only; use :meth:`als`.

        The sync default's ``asyncio.to_thread`` bridge would re-enter the running
        loop, so the skills loader and filesystem tools call :meth:`als`.
        """
        raise NotImplementedError("TemplateSkillsBackend is async-only; use als/aread/adownload_files.")

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Read a skill file's raw content for the requested line window.

        The provider's listing is authoritative for existence: an unlisted path is
        reported not found without a fetch. A listed key is read as raw bytes
        (never jinja-evaluated). A binary file returns its full base64 payload
        (``offset``/``limit`` apply to text only); a text file whose bytes are not
        UTF-8 returns an error result for this read. Line-number formatting is
        applied by the middleware.
        """
        key = _backend_path_to_key(file_path)
        if key not in await self._list_skill_keys():
            return ReadResult(error=f"File '{file_path}' not found")
        data = await _load_skill_bytes(key)
        if _is_binary_path(file_path):
            encoded = base64.standard_b64encode(data).decode("ascii")
            return ReadResult(file_data=create_file_data(encoded, encoding="base64"))
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            return ReadResult(error=f"Error reading file '{file_path}': {exc}")
        file_data = create_file_data(content, encoding="utf-8")
        sliced = slice_read_response(file_data, offset, limit)
        if isinstance(sliced, ReadResult):
            return sliced
        return ReadResult(file_data={"content": sliced, "encoding": "utf-8"})

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Async-only; use :meth:`aread`."""
        raise NotImplementedError("TemplateSkillsBackend is async-only; use als/aread/adownload_files.")

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download raw skill files, one response per path (order preserved).

        The provider's listing is authoritative: an unlisted path is reported as
        ``file_not_found`` for that path without a fetch, so a partial set still
        yields the skills that exist. A listed key returns its raw bytes.
        """
        listed = set(await self._list_skill_keys())
        responses: list[FileDownloadResponse] = []
        for path in paths:
            key = _backend_path_to_key(path)
            if key not in listed:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
                continue
            responses.append(FileDownloadResponse(path=path, content=await _load_skill_bytes(key), error=None))
        return responses

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Async-only; use :meth:`adownload_files`."""
        raise NotImplementedError("TemplateSkillsBackend is async-only; use als/aread/adownload_files.")

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        """Find served skill-bundle files matching a glob pattern.

        Globs over EVERY served file key (``SKILL.md`` plus bundled reference
        files and scripts), not only ``SKILL.md``.
        """
        skill_paths = [_key_to_backend_path(key) for key in await self._list_skill_keys()]
        matches = [
            FileInfo(path=p, is_dir=False, size=0, modified_at="")
            for p in _glob_skill_paths(skill_paths, pattern, path)
        ]
        return GlobResult(matches=matches)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        """Async-only; use :meth:`aglob`."""
        raise NotImplementedError("TemplateSkillsBackend is async-only; use als/aread/adownload_files.")

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        """Search the text skill files for a literal text pattern (binary files are not searched)."""
        files = await _text_file_index(await self._list_skill_keys())
        return grep_matches_from_files(files, pattern, path, glob)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        """Async-only; use :meth:`agrep`."""
        raise NotImplementedError("TemplateSkillsBackend is async-only; use als/aread/adownload_files.")

    # -- read-only: every mutation raises loudly --------------------------------

    def write(self, file_path: str, content: str) -> WriteResult:
        """Reject writes — skills are managed via the ``upload_template`` tool."""
        raise PermissionError(
            f"Skills are read-only: cannot write {SKILLS_ROOT}{file_path.lstrip('/')}. "
            f"Manage skills with the upload_template tool."
        )

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Reject writes — skills are managed via the ``upload_template`` tool."""
        return self.write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """Reject edits — skills are managed via the ``upload_template`` tool."""
        raise PermissionError(
            f"Skills are read-only: cannot edit {SKILLS_ROOT}{file_path.lstrip('/')}. "
            f"Manage skills with the upload_template tool."
        )

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """Reject edits — skills are managed via the ``upload_template`` tool."""
        return self.edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Reject uploads — skills are managed via the ``upload_template`` tool."""
        raise PermissionError(
            "Skills are read-only: cannot upload into the skills backend. Manage skills with the upload_template tool."
        )

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Reject uploads — skills are managed via the ``upload_template`` tool."""
        return self.upload_files(files)


def _inline_name_from_path(backend_path: str) -> str:
    """Return the skill name from a backend path rooted at the skills mount.

    ``CompositeBackend`` strips the :data:`SKILLS_ROOT` route prefix, so this
    backend receives paths like ``"/jq/SKILL.md"`` or ``"/jq/"``. The skill name
    is the first path segment (``"jq"``).
    """
    return backend_path.lstrip("/").split("/", 1)[0]


class InlineSkillsBackend(BackendProtocol):
    """Read-only skills backend overlaying call-supplied skills on the template store.

    Maps each inline skill name to its ``SKILL.md`` body and, for any requested
    skills path, serves the inline body when the name matches, otherwise delegating
    to a :class:`TemplateSkillsBackend`. Listing operations return the UNION of
    inline names and template skills. Inline content is served raw (never
    jinja-rendered); every write/edit/upload path raises loudly.
    """

    def __init__(self, inline_skills: dict[str, str]) -> None:
        """Store the inline ``name -> SKILL.md content`` map and the delegate :class:`TemplateSkillsBackend`.

        The delegate serves every name not supplied inline.
        """
        self._inline = dict(inline_skills)
        self._template = TemplateSkillsBackend()

    def _inline_skill_md_path(self, name: str) -> str:
        """Backend path of an inline skill's ``SKILL.md`` (e.g. ``"/jq/SKILL.md"``)."""
        return f"/{name}/{_SKILL_FILENAME}"

    def _inline_skill_dir(self, name: str) -> str:
        """Backend path of an inline skill's directory (e.g. ``"/jq/"``)."""
        return f"/{name}/"

    async def als(self, path: str) -> LsResult:
        """List the direct children of ``path`` across inline and template skills.

        Delegates to the template backend's direct-children listing (files,
        synthesized bundle subdirs, and the skill-directory self entry) and
        overlays each inline skill's ``is_dir`` directory entry at or below
        ``path`` — an inline skill is a single ``SKILL.md`` body, so it
        contributes only its directory (the middleware discovers it by
        downloading ``<dir>/SKILL.md``, which the inline overlay serves). An
        inline name shadows a template skill of the same name.
        """
        normalized = path if path.endswith("/") else path + "/"
        entries: dict[str, FileInfo] = {}
        template_result = await self._template.als(path)
        for entry in template_result.entries or []:
            entries[entry["path"]] = entry
        for name in self._inline:
            skill_dir = self._inline_skill_dir(name)
            if skill_dir.startswith(normalized):
                entries[skill_dir] = FileInfo(path=skill_dir, is_dir=True, size=0, modified_at="")
        ordered = sorted(entries.values(), key=lambda info: info["path"])
        return LsResult(entries=ordered)

    def ls(self, path: str) -> LsResult:
        """Async-only; use :meth:`als`."""
        raise NotImplementedError("InlineSkillsBackend is async-only; use als/aread/adownload_files.")

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Read an inline or template skill file for the requested line window.

        Inline content is returned raw for a matching name; every other path
        delegates to the template backend (also raw).
        """
        name = _inline_name_from_path(file_path)
        if name in self._inline and file_path == self._inline_skill_md_path(name):
            file_data = create_file_data(self._inline[name], encoding="utf-8")
            sliced = slice_read_response(file_data, offset, limit)
            if isinstance(sliced, ReadResult):
                return sliced
            return ReadResult(file_data={"content": sliced, "encoding": "utf-8"})
        return await self._template.aread(file_path, offset, limit)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        """Async-only; use :meth:`aread`."""
        raise NotImplementedError("InlineSkillsBackend is async-only; use als/aread/adownload_files.")

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Download raw inline or template skill files, one response per path.

        An inline name's ``SKILL.md`` is served from the inline map; every other
        path delegates to the template backend, which reports a key absent from
        the provider's listing as ``file_not_found`` for that path.
        """
        inline_responses: dict[int, FileDownloadResponse] = {}
        delegated_indices: list[int] = []
        delegated_paths: list[str] = []
        for index, path in enumerate(paths):
            name = _inline_name_from_path(path)
            if name in self._inline and path == self._inline_skill_md_path(name):
                inline_responses[index] = FileDownloadResponse(
                    path=path, content=self._inline[name].encode("utf-8"), error=None
                )
            else:
                delegated_indices.append(index)
                delegated_paths.append(path)
        delegated_responses = (
            dict(zip(delegated_indices, await self._template.adownload_files(delegated_paths), strict=True))
            if delegated_paths
            else {}
        )
        return [
            inline_responses[index] if index in inline_responses else delegated_responses[index]
            for index in range(len(paths))
        ]

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Async-only; use :meth:`adownload_files`."""
        raise NotImplementedError("InlineSkillsBackend is async-only; use als/aread/adownload_files.")

    async def aglob(self, pattern: str, path: str = "/") -> GlobResult:
        """Find inline and template skill files matching a glob.

        Unions the template backend's served files (whole bundles) with each
        inline skill's ``SKILL.md`` path.
        """
        template_matches = await self._template.aglob(pattern, path)
        matched_paths = {match["path"] for match in template_matches.matches or []}
        inline_paths = [self._inline_skill_md_path(name) for name in self._inline]
        matched_paths.update(_glob_skill_paths(inline_paths, pattern, path))
        matches = [FileInfo(path=p, is_dir=False, size=0, modified_at="") for p in sorted(matched_paths)]
        return GlobResult(matches=matches)

    def glob(self, pattern: str, path: str = "/") -> GlobResult:
        """Async-only; use :meth:`aglob`."""
        raise NotImplementedError("InlineSkillsBackend is async-only; use als/aread/adownload_files.")

    async def agrep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        """Search inline and template text skill files for a literal text pattern.

        A template skill shadowed by an inline name is not read; binary template
        files are not searched.
        """
        unshadowed = [
            key
            for key in await self._template._list_skill_keys()
            if key[len(_SKILLS_KEY_PREFIX) :].split("/", 1)[0] not in self._inline
        ]
        files = await _text_file_index(unshadowed)
        for name, content in self._inline.items():
            files[self._inline_skill_md_path(name)] = create_file_data(content, encoding="utf-8")
        return grep_matches_from_files(files, pattern, path, glob)

    def grep(self, pattern: str, path: str | None = None, glob: str | None = None) -> GrepResult:
        """Async-only; use :meth:`agrep`."""
        raise NotImplementedError("InlineSkillsBackend is async-only; use als/aread/adownload_files.")

    # -- read-only: every mutation raises loudly --------------------------------

    def write(self, file_path: str, content: str) -> WriteResult:
        """Reject writes — skills are read-only (managed via upload_template or inline)."""
        raise PermissionError(
            f"Skills are read-only: cannot write {SKILLS_ROOT}{file_path.lstrip('/')}. "
            f"Manage skills with the upload_template tool or supply them inline."
        )

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        """Reject writes — skills are read-only (managed via upload_template or inline)."""
        return self.write(file_path, content)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """Reject edits — skills are read-only (managed via upload_template or inline)."""
        raise PermissionError(
            f"Skills are read-only: cannot edit {SKILLS_ROOT}{file_path.lstrip('/')}. "
            f"Manage skills with the upload_template tool or supply them inline."
        )

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        """Reject edits — skills are read-only (managed via upload_template or inline)."""
        return self.edit(file_path, old_string, new_string, replace_all)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Reject uploads — skills are read-only (managed via upload_template or inline)."""
        raise PermissionError(
            "Skills are read-only: cannot upload into the skills backend. "
            "Manage skills with the upload_template tool or supply them inline."
        )

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Reject uploads — skills are read-only (managed via upload_template or inline)."""
        return self.upload_files(files)


def build_backend(inline_skills: dict[str, str] | None = None) -> CompositeBackend:
    """Build the composite backend for a deep agent.

    ``default`` (everything except skills) uses a :class:`StateBackend`: scratch
    files live in graph state, so concurrent sessions never collide and the files
    are checkpointed with the conversation.

    :data:`SKILLS_ROOT` routes to a read-only skills backend serving skills live from
    ``tai42_app.storage.resource_manager``. When ``inline_skills`` is supplied it is
    an :class:`InlineSkillsBackend` overlaying that content on the template store.
    """
    skills_backend: BackendProtocol = InlineSkillsBackend(inline_skills) if inline_skills else TemplateSkillsBackend()
    return CompositeBackend(
        default=StateBackend(),
        routes={SKILLS_ROOT: skills_backend},
    )
