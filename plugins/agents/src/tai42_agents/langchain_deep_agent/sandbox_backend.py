"""The durable scratch backend — ``SandboxSessionBackend`` over a ``SandboxSession``.

The deep agent's scratch filesystem moves out of in-graph ``StateBackend`` state onto
a durable sandbox WORKSPACE volume: a :class:`SandboxSessionBackend` subclasses
deepagents' :class:`~deepagents.backends.sandbox.BaseSandbox` and delegates the three
runtime primitives to a tai42 :class:`~tai42_contract.sandbox.SandboxSession`, letting
``BaseSandbox`` derive ls/read/glob/grep/edit/write for free (it builds server-side
``sh``/``python3`` scripts and runs them via ``aexecute``). This makes the deepagents
built-in ``execute`` tool a LIVE durable shell (dormant under ``StateBackend``) and the
scratch tree durable across a threaded run's turns.

ROOTING: the agent's non-skills tree is rooted at ``f"{session.workspace_path}/project"``
— provider-agnostic (``/workspace/project`` under a container provider,
``<root>/<workspace_key>/project`` under the direct-host provider). Every exec runs with
that project dir as its cwd and every file-transfer path resolves under it, so a
credential directory the adapter writes OUTSIDE ``project`` (``f"{ws}/.creds"``, the deep
agent's bearer-cred path) is never reachable through the agent's file tools.

``BaseSandbox`` marks FOUR members abstract — the SYNC ``execute`` / ``upload_files`` /
``download_files`` and the ``id`` property. The deep agent drives only the async faces
(re-entering the running loop through the sync ``asyncio.to_thread`` bridge would
deadlock — the same reason ``TemplateSkillsBackend`` is async-only), so the three sync
abstracts raise ``NotImplementedError`` and the async trio the file ops actually call
(``aexecute`` / ``aupload_files`` / ``adownload_files``) plus ``id`` are overridden here.
``BaseSandbox``'s ls/read/glob/grep/edit/write all route through ``aexecute`` /
``aupload_files``, never the sync trio.
"""

from __future__ import annotations

from deepagents.backends import BackendProtocol, CompositeBackend
from deepagents.backends.protocol import (
    EditResult,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.sandbox import BaseSandbox
from tai42_contract.sandbox import SandboxError, SandboxSession

# Read-only imports of the sibling-owned skills backend + mount point (Part D widens the
# skills backend there; this module never edits it) — reused verbatim so the durable
# composite routes skills exactly as the non-sandbox ``build_backend`` does.
from tai42_agents.langchain_deep_agent.backend import (
    SKILLS_ROOT,
    InlineSkillsBackend,
    TemplateSkillsBackend,
)

# Mirrors the kit ``SandboxDispatchSettings.exec_default_timeout_seconds`` default — the
# SHORT-helper ceiling for a single derived file op (ls/read/glob/grep/edit/write), NOT a
# coding-turn wall-clock. Deliberately a plain constant: the agents plugin declares no
# sandbox-dispatch settings group of its own, and this is only the fallback used when
# deepagents passes no per-call ``timeout``.
_DEFAULT_EXEC_TIMEOUT_SECONDS = 300

_ASYNC_ONLY = "SandboxSessionBackend is async-only; use aexecute/aupload_files/adownload_files."

# deepagents' ``BaseSandbox`` large-edit fallback (``_aedit_via_upload``, taken when the combined
# ``old_string`` + ``new_string`` payload exceeds ``_EDIT_INLINE_MAX_BYTES`` = 50_000) uploads its
# old/new scratch strings to ABSOLUTE ``/tmp/.deepagents_edit_<uid>_{old,new}`` paths and BAKES
# those SAME absolute paths into the server-side replace script it runs via ``aexecute``. Those are
# engine-internal, out-of-tree temp paths (never an agent virtual path), so ``_rooted`` must leave
# them at their absolute location: re-basing them under ``project`` would diverge the uploaded file
# from the script's read path and the >50KB edit would fail SILENTLY (``temp_read_failed``, the
# target left unmodified). Verified against deepagents 0.7.5 ``backends/sandbox.py``.
_DEEPAGENTS_ENGINE_TMP_PREFIX = "/tmp/.deepagents_edit_"  # deepagents' own scratch prefix, not ours


class SandboxSessionBackend(BaseSandbox):
    """A deepagents ``BaseSandbox`` whose primitives run in a tai42 ``SandboxSession``.

    Holds the live session and the project root; delegates ``aexecute`` to
    ``session.exec``, ``aupload_files`` to ``session.put_file``, ``adownload_files`` to
    ``session.get_file``, and ``id`` to ``session.id``. Everything else
    (ls/read/glob/grep/edit/write) is inherited from ``BaseSandbox``.
    """

    def __init__(self, session: SandboxSession) -> None:
        self._session = session
        # The agent's writable tree, absolute under the provider's workspace root. Built
        # from ``session.workspace_path`` (never a hardcoded ``/workspace``) so the same
        # backend is provider-agnostic.
        self._root = f"{session.workspace_path}/project"
        self._root_ready = False

    # -- sync abstracts: unreachable on the async drive ----------------------------

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Async-only; use :meth:`aexecute`."""
        raise NotImplementedError(_ASYNC_ONLY)

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Async-only; use :meth:`aupload_files`."""
        raise NotImplementedError(_ASYNC_ONLY)

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Async-only; use :meth:`adownload_files`."""
        raise NotImplementedError(_ASYNC_ONLY)

    # -- identity ------------------------------------------------------------------

    @property
    def id(self) -> str:
        return self._session.id

    # -- path rooting --------------------------------------------------------------

    def _rooted(self, file_path: str) -> str:
        """Resolve a deepagents file path to an ABSOLUTE path under the project root.

        deepagents 0.7.5 addresses the default (non-skills) backend with ABSOLUTE VIRTUAL
        paths anchored at ``/`` (verified against ``CompositeBackend`` — the default route
        receives the original path, e.g. ``/plan.md``; the example is
        ``composite.write("/temp.txt", ...)``), and it BAKES those paths into the server-side
        ``sh``/``python3`` scripts it runs via ``aexecute`` — where an absolute path resolves
        against the session filesystem root, NOT the exec cwd. So rooting only the file
        transfers (as the write path did) would leave the derived ls/read/glob/grep/edit
        reading the provider's real root. Every path-taking op is therefore re-based HERE, to
        an absolute path under ``{workspace_path}/project``, so a write and its read resolve to
        the SAME place regardless of cwd.

        A relative path joins under the root; an absolute virtual path is re-based (leading
        ``/`` stripped, joined under the root). The mapping is IDEMPOTENT: a path already under
        the root passes through unchanged, so a listing result the model reads back (deepagents
        reports the rooted path) is never double-rooted.

        deepagents' OWN engine scratch paths (the ``/tmp/.deepagents_edit_*`` temp files the
        large-edit fallback uploads and reads via a baked-in absolute path) are the exception: they
        pass through UN-rebased so the upload and the server-side read agree (see
        ``_DEEPAGENTS_ENGINE_TMP_PREFIX``). Those are out-of-tree by construction — never an agent
        virtual path — so they can never collide with the agent's project tree."""
        if file_path.startswith(_DEEPAGENTS_ENGINE_TMP_PREFIX):
            return file_path
        if file_path == self._root or file_path.startswith(self._root + "/"):
            return file_path
        return f"{self._root}/{file_path.lstrip('/')}"

    async def _ensure_root(self) -> None:
        """Create the project root once (idempotent) so an exec can cwd into it. Runs
        with the default (workspace-root) cwd, which always exists."""
        if self._root_ready:
            return
        await self._session.exec(["mkdir", "-p", self._root], timeout_seconds=_DEFAULT_EXEC_TIMEOUT_SECONDS)
        self._root_ready = True

    # -- derived file ops: re-base the deepagents virtual path under project ---------
    #
    # ``BaseSandbox`` derives these from ``aexecute`` (baking the path into a server-side
    # script) / ``aupload_files``; each is overridden ONLY to re-base its path argument onto
    # the project root before delegating, so an absolute virtual path lands in the agent's
    # tree. ``aexecute`` still runs with the project dir as cwd, so the built-in ``execute``
    # shell tool's own relative commands resolve there too.

    async def als(self, path: str) -> LsResult:
        return await super().als(self._rooted(path))

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return await super().aread(self._rooted(file_path), offset, limit)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        return await super().awrite(self._rooted(file_path), content)

    async def aedit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        return await super().aedit(self._rooted(file_path), old_string, new_string, replace_all)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        return await super().aglob(pattern, self._rooted(path) if path is not None else self._root)

    async def agrep(
        self, pattern: str, path: str | None = None, glob: str | None = None, *, max_count: int | None = None
    ) -> GrepResult:
        rooted = self._rooted(path) if path is not None else self._root
        return await super().agrep(pattern, rooted, glob, max_count=max_count)

    # -- async primitives ----------------------------------------------------------

    async def aexecute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        """Run ``command`` in the session's project dir and map the result.

        Runs ``sh -lc <command>`` with the project root as cwd, so the deepagents-derived
        ls/read/glob/grep/edit scripts and the built-in ``execute`` shell both operate on
        the agent's tree. The tai42 :class:`~tai42_contract.sandbox.ExecResult`'s
        ``stdout``/``stderr`` are combined into ``ExecuteResponse.output`` (the shape
        deepagents' parsers consume); ``truncated`` is ``False`` (the session streams the
        whole result)."""
        await self._ensure_root()
        result = await self._session.exec(
            ["sh", "-lc", command],
            cwd=self._root,
            timeout_seconds=timeout if timeout is not None else _DEFAULT_EXEC_TIMEOUT_SECONDS,
        )
        return ExecuteResponse(output=result.stdout + result.stderr, exit_code=result.exit_code, truncated=False)

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        """Write each ``(path, data)`` into the project tree via ``session.put_file``.

        A write failure is reported per-file as a ``FileUploadResponse`` error (batch
        partial-success, the deepagents contract) rather than raising."""
        responses: list[FileUploadResponse] = []
        for path, data in files:
            try:
                await self._session.put_file(self._rooted(path), data)
                responses.append(FileUploadResponse(path=path, error=None))
            except SandboxError as exc:
                responses.append(FileUploadResponse(path=path, error=str(exc)))
        return responses

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        """Read each path from the project tree via ``session.get_file``.

        A miss maps to ``FileDownloadResponse(error="file_not_found")`` for that path (the
        normalized deepagents literal), so a partial batch still yields the files present."""
        responses: list[FileDownloadResponse] = []
        for path in paths:
            try:
                content = await self._session.get_file(self._rooted(path))
                responses.append(FileDownloadResponse(path=path, content=content, error=None))
            except SandboxError:
                responses.append(FileDownloadResponse(path=path, content=None, error="file_not_found"))
        return responses


def build_sandbox_backend(session: SandboxSession, inline_skills: dict[str, str] | None = None) -> CompositeBackend:
    """The deep agent's DURABLE composite backend for a live run: scratch on the sandbox
    workspace volume, skills read-only over the template store.

    ``default`` (everything except skills) is a :class:`SandboxSessionBackend` over ``session``
    — the ``StateBackend``→``SandboxSessionBackend`` swap (§B2), which makes deepagents' built-in
    ``execute`` tool a LIVE durable shell and moves scratch out of graph state onto the volume.
    ``routes={SKILLS_ROOT: skills_backend}`` is unchanged from the non-sandbox
    :func:`~tai42_agents.langchain_deep_agent.backend.build_backend`: skills stay read-only over
    the template store (an :class:`InlineSkillsBackend` when inline content is supplied, else a
    :class:`TemplateSkillsBackend`).

    Composed HERE rather than folding the session into ``backend.build_backend`` so the durable
    default lives beside its :class:`SandboxSessionBackend`; the append path keeps the
    non-sandbox ``build_backend`` (StateBackend), so a checkpoint-only write needs no session."""
    skills_backend: BackendProtocol = InlineSkillsBackend(inline_skills) if inline_skills else TemplateSkillsBackend()
    return CompositeBackend(default=SandboxSessionBackend(session), routes={SKILLS_ROOT: skills_backend})
