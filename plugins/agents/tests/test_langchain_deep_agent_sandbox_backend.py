"""``SandboxSessionBackend`` over the fake sandbox provider.

Proves the deep agent's durable scratch backend derives ls/read/write/edit/glob/grep from
the tai42 :class:`~tai42_contract.sandbox.SandboxSession` primitives (the built-in
``execute`` shell runs a command via the session), roots the agent tree under
``{workspace_path}/project``, and — because scratch now lives on the workspace VOLUME —
survives across two threaded turns on a persistent workspace while an ephemeral one dies
with its session. The four sync abstracts raise (async-only); ``id`` delegates to the
session.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from tai42_contract.sandbox import SandboxError, SandboxSession, SandboxSessionSpec
from tests._sandbox_fake import FakeSandbox, make_fake_sandbox

from tai42_agents.langchain_deep_agent.sandbox_backend import SandboxSessionBackend


def _spec(workspace_key: str, *, durability: str = "ephemeral") -> SandboxSessionSpec:
    return SandboxSessionSpec(
        image="registry.example/lean@sha256:" + "a" * 64,
        workspace_key=workspace_key,
        durability=durability,  # pyright: ignore[reportArgumentType]
        network="egress",
        ttl_seconds=300,
    )


async def _session(sandbox: FakeSandbox, workspace_key: str, *, durability: str = "ephemeral") -> SandboxSession:
    return await sandbox.create_session(_spec(workspace_key, durability=durability))


@pytest.fixture
def sandbox() -> Iterator[FakeSandbox]:
    sbx = make_fake_sandbox()
    yield sbx
    sbx.dispose()


def test_write_read_round_trip(sandbox: FakeSandbox) -> None:
    async def run() -> str:
        backend = SandboxSessionBackend(await _session(sandbox, "ws-rt"))
        write = await backend.awrite("notes.txt", "hello durable world")
        assert write.error is None
        read = await backend.aread("notes.txt")
        assert read.error is None
        return read.file_data["content"] if read.file_data else ""

    assert "hello durable world" in asyncio.run(run())


def test_ls_lists_written_files(sandbox: FakeSandbox) -> None:
    async def run() -> set[str]:
        backend = SandboxSessionBackend(await _session(sandbox, "ws-ls"))
        await backend.awrite("a.txt", "A")
        await backend.awrite("sub/b.txt", "B")
        result = await backend.als(".")
        return {entry["path"].rsplit("/", 1)[-1] for entry in (result.entries or [])}

    names = asyncio.run(run())
    assert "a.txt" in names
    assert "sub" in names


def test_glob_and_grep(sandbox: FakeSandbox) -> None:
    async def run() -> tuple[int, int]:
        backend = SandboxSessionBackend(await _session(sandbox, "ws-gg"))
        await backend.awrite("one.py", "import os\nvalue = 1\n")
        await backend.awrite("two.py", "value = 2\n")
        glob = await backend.aglob("*.py", ".")
        grep = await backend.agrep("value", ".", "*.py")
        return len(glob.matches or []), len(grep.matches or [])

    globbed, grepped = asyncio.run(run())
    assert globbed == 2
    assert grepped >= 2


def test_edit_modifies_file(sandbox: FakeSandbox) -> None:
    async def run() -> str:
        backend = SandboxSessionBackend(await _session(sandbox, "ws-edit"))
        await backend.awrite("f.txt", "the quick brown fox")
        edit = await backend.aedit("f.txt", "brown", "red")
        assert edit.error is None
        read = await backend.aread("f.txt")
        return read.file_data["content"] if read.file_data else ""

    assert "red" in asyncio.run(run())


def test_execute_runs_command_via_session(sandbox: FakeSandbox) -> None:
    async def run() -> str:
        backend = SandboxSessionBackend(await _session(sandbox, "ws-exec"))
        response = await backend.aexecute("echo durable-shell-live")
        assert response.exit_code == 0
        return response.output

    assert "durable-shell-live" in asyncio.run(run())


def test_download_miss_reports_file_not_found(sandbox: FakeSandbox) -> None:
    async def run() -> str | None:
        backend = SandboxSessionBackend(await _session(sandbox, "ws-miss"))
        responses = await backend.adownload_files(["absent.txt"])
        return responses[0].error

    assert asyncio.run(run()) == "file_not_found"


def test_sync_methods_raise_async_only(sandbox: FakeSandbox) -> None:
    async def build() -> SandboxSessionBackend:
        return SandboxSessionBackend(await _session(sandbox, "ws-sync"))

    backend = asyncio.run(build())
    with pytest.raises(NotImplementedError):
        backend.execute("echo hi")
    with pytest.raises(NotImplementedError):
        backend.upload_files([("x", b"y")])
    with pytest.raises(NotImplementedError):
        backend.download_files(["x"])


def test_id_delegates_to_session(sandbox: FakeSandbox) -> None:
    async def run() -> tuple[str, str]:
        session = await _session(sandbox, "ws-id")
        return SandboxSessionBackend(session).id, session.id

    backend_id, session_id = asyncio.run(run())
    assert backend_id == session_id


def test_absolute_virtual_paths_rebase_under_project(sandbox: FakeSandbox) -> None:
    """deepagents 0.7.5 addresses the default backend with ABSOLUTE VIRTUAL paths anchored at
    ``/`` (verified against ``CompositeBackend`` — the default route receives the original path,
    e.g. ``/plan.md``) and BAKES them into the server-side scripts it runs via ``aexecute``,
    where an absolute path ignores the exec cwd. The backend must therefore re-base absolute
    paths under ``{workspace_path}/project`` for EVERY op, not only the file transfers — so a
    write at ``/plan.md`` and its read/ls/glob/grep/edit resolve to the SAME place. This is the
    plan's flagged path-convention verification, exercised over the live deep-agent path space."""

    async def run() -> tuple[str, set[str], int, int, str]:
        backend = SandboxSessionBackend(await _session(sandbox, "ws-abs"))
        # Write with an ABSOLUTE virtual path (what deepagents emits), including a nested dir.
        assert (await backend.awrite("/plan.md", "the durable plan")).error is None
        assert (await backend.awrite("/src/app.py", "value = 1\n")).error is None

        # Read it back by the SAME absolute path — the derived read exec must root it too.
        read = await backend.aread("/plan.md")
        assert read.error is None
        content = read.file_data["content"] if read.file_data else ""

        # ls "/" must list the PROJECT tree (rooted), never the provider filesystem root.
        listing = await backend.als("/")
        names = {entry["path"].rsplit("/", 1)[-1] for entry in (listing.entries or [])}

        glob = await backend.aglob("*.md", "/")
        grep = await backend.agrep("value", "/src", "*.py")

        # An edit by absolute path resolves to the same file the write created.
        edit = await backend.aedit("/plan.md", "durable", "committed")
        assert edit.error is None
        edited = await backend.aread("/plan.md")
        edited_content = edited.file_data["content"] if edited.file_data else ""
        return content, names, len(glob.matches or []), len(grep.matches or []), edited_content

    content, names, globbed, grepped, edited = asyncio.run(run())
    assert "the durable plan" in content
    assert "plan.md" in names
    assert "src" in names
    assert globbed == 1
    assert grepped >= 1
    assert "committed" in edited


def _honor_absolute_paths(session: SandboxSession) -> SandboxSession:
    """Rebind ``put_file``/``get_file`` so an ABSOLUTE path is honored LITERALLY (real host FS), as
    a real container provider does per the PATH CONTRACT (absolute = as given).

    The base fake conservatively rejects any absolute path OUTSIDE its temp workspace, but
    deepagents' >50KB edit fallback (``_aedit_via_upload``) uploads its old/new scratch to ABSOLUTE
    ``/tmp/.deepagents_edit_*`` and BAKES those SAME absolute paths into the server-side replace
    script — so a faithful provider must let the upload and the script's read resolve to the one
    absolute location. The fake's ``exec`` already runs as a real host subprocess reading real
    ``/tmp``, so honoring the absolute ``put`` here closes the loop end-to-end."""
    base_put = session.put_file
    base_get = session.get_file

    async def put_file(path: str, data: bytes) -> None:
        if os.path.isabs(path):
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return
        await base_put(path, data)

    async def get_file(path: str) -> bytes:
        if os.path.isabs(path):
            try:
                return Path(path).read_bytes()
            except OSError as exc:
                raise SandboxError(f"absolute get_file miss for {path!r}") from exc
        return await base_get(path)

    session.put_file = put_file  # type: ignore[method-assign]
    session.get_file = get_file  # type: ignore[method-assign]
    return session


def test_large_edit_over_inline_threshold_actually_modifies_the_file(sandbox: FakeSandbox) -> None:
    """A >50KB edit routes through deepagents' ``_aedit_via_upload`` fallback: it uploads old/new to
    ABSOLUTE ``/tmp/.deepagents_edit_*`` scratch paths and bakes those SAME absolute paths into the
    server-side replace script. ``_rooted`` MUST leave those engine temp paths absolute (never
    re-base them under ``project``) — otherwise the uploaded file and the script's read path DIVERGE
    and the edit fails SILENTLY (``temp_read_failed``, the target left unmodified). This drives the
    whole large-edit path end-to-end and asserts the file content is ACTUALLY modified (it fails on
    a backend that re-bases the temp paths)."""

    async def run() -> tuple[str | None, str]:
        session = _honor_absolute_paths(await _session(sandbox, "ws-bigedit"))
        backend = SandboxSessionBackend(session)
        # Combined old+new = 120_000 bytes, over _EDIT_INLINE_MAX_BYTES (50_000): forces the upload
        # fallback rather than the inline server-side edit.
        old = "OLD" * 20_000
        new = "NEW" * 20_000
        assert (await backend.awrite("big.txt", f"<head>{old}<tail>")).error is None
        edit = await backend.aedit("big.txt", old, new)
        read = await backend.aread("big.txt")
        content = read.file_data["content"] if read.file_data else ""
        return edit.error, content

    error, content = asyncio.run(run())
    assert error is None
    assert "NEW" * 100 in content
    assert "OLD" not in content


def test_aupload_files_maps_a_put_failure_to_a_response_error(sandbox: FakeSandbox) -> None:
    """A per-file ``put_file`` failure surfaces as a ``FileUploadResponse`` error (batch
    partial-success, the deepagents contract), never a raise — so an upload that hits a provider
    error is reported LOUDLY per file rather than dropped."""

    async def run() -> tuple[str, str | None]:
        session = await _session(sandbox, "ws-upfail")

        async def failing_put(path: str, data: bytes) -> None:
            raise SandboxError("disk full")

        session.put_file = failing_put  # type: ignore[method-assign]
        backend = SandboxSessionBackend(session)
        responses = await backend.aupload_files([("notes.txt", b"data")])
        return responses[0].path, responses[0].error

    path, error = asyncio.run(run())
    assert path == "notes.txt"
    assert error is not None
    assert "disk full" in error


def test_scratch_survives_across_turns_on_persistent_workspace(sandbox: FakeSandbox) -> None:
    """A persistent workspace keeps the scratch tree across two sessions on the same
    ``workspace_key`` — the durability the ``StateBackend``→``SandboxSessionBackend`` swap
    buys — while an ephemeral one starts empty each turn."""

    async def run() -> tuple[str, str | None]:
        first = await _session(sandbox, "durable-ws", durability="persistent")
        await SandboxSessionBackend(first).awrite("kept.txt", "survives the turn")
        await sandbox.destroy_session_via_reap(first.id)

        second = await _session(sandbox, "durable-ws", durability="persistent")
        read = await SandboxSessionBackend(second).aread("kept.txt")
        durable = read.file_data["content"] if read.file_data else ""

        ephemeral_first = await _session(sandbox, "scratch-ws", durability="ephemeral")
        await SandboxSessionBackend(ephemeral_first).awrite("gone.txt", "dies with the session")
        await sandbox.destroy_session_via_reap(ephemeral_first.id)
        ephemeral_second = await _session(sandbox, "scratch-ws", durability="ephemeral")
        missing = await SandboxSessionBackend(ephemeral_second).adownload_files(["gone.txt"])
        return durable, missing[0].error

    durable, ephemeral_error = asyncio.run(run())
    assert "survives the turn" in durable
    assert ephemeral_error == "file_not_found"
