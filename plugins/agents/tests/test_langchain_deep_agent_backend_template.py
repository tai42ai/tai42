"""``langchain_deep_agent`` template-skill backend: build shape, read/list/download,
write refusals, and the aglob/agrep/aread filesystem operations.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from deepagents.backends import CompositeBackend, StateBackend
from tai42_contract.app import tai42_app
from tests._langchain_deep_agent_backend_support import (
    _FakeResourceManager,
    _TemplateMissingError,
)

from tai42_agents.langchain_deep_agent.backend import (
    SKILLS_ROOT,
    TemplateSkillsBackend,
    build_backend,
)


@pytest.fixture
def fake_tm(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceManager:
    tm = _FakeResourceManager()
    monkeypatch.setattr(tai42_app.storage, "resource_manager", tm)
    return tm


def test_build_backend_shape() -> None:
    backend = build_backend()
    assert isinstance(backend, CompositeBackend)
    assert isinstance(backend.default, StateBackend)
    assert SKILLS_ROOT in backend.routes
    assert isinstance(backend.routes[SKILLS_ROOT], TemplateSkillsBackend)


def test_skill_uploaded_to_provider_reads_back_through_backend(fake_tm: _FakeResourceManager) -> None:
    """A skill uploaded under the ``skills/`` key convention is read live back
    through the composite backend at its ``/skills/<name>/SKILL.md`` path."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "hello")
        backend = build_backend()
        return await backend.aread(f"{SKILLS_ROOT}jq/SKILL.md")

    res = asyncio.run(go())
    assert res.error is None
    assert res.file_data["content"] == "hello"


def test_read_missing_skill_reports_not_found(fake_tm: _FakeResourceManager) -> None:
    async def go() -> Any:
        return await build_backend().aread(f"{SKILLS_ROOT}missing/SKILL.md")

    res = asyncio.run(go())
    assert res.error is not None
    assert "not found" in res.error


def test_als_lists_skill_dirs_for_middleware(fake_tm: _FakeResourceManager) -> None:
    """The skills middleware discovers a source by enumerating the ``is_dir``
    entries ``als`` returns for it and downloading ``<dir>/SKILL.md`` from each.

    Listing the whole mount yields one is_dir entry per skill dir; listing a
    per-skill source ``/skills/<name>/`` yields that dir's OWN is_dir entry (the
    self entry that keeps the source discoverable) alongside its children."""

    async def go() -> tuple[Any, Any]:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        await fake_tm.upload_template("skills/flow/SKILL.md", "y")
        backend = build_backend()
        whole = await backend.als(SKILLS_ROOT)
        single = await backend.als(f"{SKILLS_ROOT}jq/")
        return whole, single

    whole, single = asyncio.run(go())
    assert {e["path"] for e in whole.entries} == {f"{SKILLS_ROOT}jq/", f"{SKILLS_ROOT}flow/"}
    assert all(e["is_dir"] for e in whole.entries)
    # The per-skill source lists its own dir (is_dir, for middleware discovery)
    # plus its direct children (here just SKILL.md).
    single_dirs = {e["path"] for e in single.entries if e["is_dir"]}
    assert f"{SKILLS_ROOT}jq/" in single_dirs
    assert f"{SKILLS_ROOT}jq/SKILL.md" in {e["path"] for e in single.entries if not e["is_dir"]}


def test_listing_filters_non_skill_keys(fake_tm: _FakeResourceManager) -> None:
    """Only keys under the ``skills/`` prefix are served — a key with a different
    prefix (``other/``) is excluded from the mount. A bundled non-SKILL.md file
    under a skill dir is served (widening) but does not add a second skill dir:
    listing the mount root collapses a skill's files into its one directory."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        await fake_tm.upload_template("skills/jq/README.md", "kept")
        await fake_tm.upload_template("other/stuff", "ignore")
        return await build_backend().als(SKILLS_ROOT)

    result = asyncio.run(go())
    assert [e["path"] for e in result.entries] == [f"{SKILLS_ROOT}jq/"]


def test_download_files_returns_raw_and_marks_missing(fake_tm: _FakeResourceManager) -> None:
    async def go() -> list[Any]:
        await fake_tm.upload_template("skills/jq/SKILL.md", "body")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.adownload_files(["/jq/SKILL.md", "/nope/SKILL.md"])

    found, missing = asyncio.run(go())
    assert found.content == b"body"
    assert found.error is None
    assert missing.content is None
    assert missing.error == "file_not_found"


def test_writes_raise(fake_tm: _FakeResourceManager) -> None:
    skills = TemplateSkillsBackend()

    async def go() -> None:
        with pytest.raises(PermissionError, match="read-only"):
            await skills.awrite("/jq/SKILL.md", "x")
        with pytest.raises(PermissionError, match="read-only"):
            await skills.aedit("/jq/SKILL.md", "a", "b")
        with pytest.raises(PermissionError, match="read-only"):
            await skills.aupload_files([("/jq/SKILL.md", b"x")])

    asyncio.run(go())


def test_sync_methods_raise_not_implemented(fake_tm: _FakeResourceManager) -> None:
    """The backend is async-only; every sync read entrypoint raises loudly so a
    sync caller fails fast instead of silently re-entering the event loop."""
    skills = TemplateSkillsBackend()
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.read("/jq/SKILL.md")
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.ls("/")
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.glob("*")
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.grep("x")
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.download_files(["/jq/SKILL.md"])


def test_skill_body_is_not_jinja_rendered(fake_tm: _FakeResourceManager) -> None:
    """Skill markdown is read raw — jinja-looking braces survive verbatim."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "use {{ not_rendered }} here")
        return await build_backend().aread(f"{SKILLS_ROOT}jq/SKILL.md")

    res = asyncio.run(go())
    assert res.file_data["content"] == "use {{ not_rendered }} here"


def test_skill_reflects_provider_overwrite(fake_tm: _FakeResourceManager) -> None:
    """Re-uploading a skill under the same key is read back as the new content —
    the backend serves the provider live, so an overwrite is reflected with no
    re-seed or cache to clear."""

    async def go() -> tuple[Any, Any]:
        await fake_tm.upload_template("skills/jq/SKILL.md", "VERSION ONE")
        first = await build_backend().aread(f"{SKILLS_ROOT}jq/SKILL.md")
        await fake_tm.upload_template("skills/jq/SKILL.md", "VERSION TWO")
        second = await build_backend().aread(f"{SKILLS_ROOT}jq/SKILL.md")
        return first, second

    first, second = asyncio.run(go())
    assert first.file_data["content"] == "VERSION ONE"
    assert second.file_data["content"] == "VERSION TWO"


def test_template_agrep_searches_skill_bodies(fake_tm: _FakeResourceManager) -> None:
    """agrep on the plain template backend finds a literal match in a skill body
    and reports the matching skill's path; a non-matching skill is excluded."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "look for FINDME here")
        await fake_tm.upload_template("skills/other/SKILL.md", "nothing to see")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.agrep("FINDME")

    result = asyncio.run(go())
    assert {m["path"] for m in result.matches} == {"/jq/SKILL.md"}


def test_template_agrep_propagates_vanished_listed_key(fake_tm: _FakeResourceManager) -> None:
    """A key the provider lists but which is absent from the store (it vanished
    between the listing and the fetch) propagates the provider's own error out of
    agrep, rather than being silently dropped from the search corpus."""

    async def go() -> None:
        fake_tm.phantom_keys.add("skills/gone/SKILL.md")
        skills = build_backend().routes[SKILLS_ROOT]
        with pytest.raises(_TemplateMissingError):
            await skills.agrep("anything")

    asyncio.run(go())


def test_vanished_listed_key_propagates_across_read_grep_download(fake_tm: _FakeResourceManager) -> None:
    """A key present in the provider's listing but absent from the store propagates
    the provider's own error out of aread, agrep, and adownload_files — a genuine
    store inconsistency surfaced loudly, never masked as a not-found value."""

    async def go() -> None:
        fake_tm.phantom_keys.add("skills/gone/SKILL.md")
        skills = build_backend().routes[SKILLS_ROOT]
        with pytest.raises(_TemplateMissingError):
            await skills.aread("/gone/SKILL.md")
        with pytest.raises(_TemplateMissingError):
            await skills.agrep("anything")
        with pytest.raises(_TemplateMissingError):
            await skills.adownload_files(["/gone/SKILL.md"])

    asyncio.run(go())


def test_template_agrep_indexes_empty_body_skill(fake_tm: _FakeResourceManager) -> None:
    """An empty-string skill body is a valid body and stays in the search corpus:
    grepping the empty pattern matches its one empty line, proving it was indexed
    (guards against an ``if content:`` truthiness check dropping it)."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/empty/SKILL.md", "")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.agrep("")

    result = asyncio.run(go())
    assert "/empty/SKILL.md" in {m["path"] for m in result.matches}


def test_template_aread_offset_beyond_file_reports_error(fake_tm: _FakeResourceManager) -> None:
    """An offset past the file length surfaces the slice error verbatim, never a
    silently empty read."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "one\ntwo\n")
        return await build_backend().aread(f"{SKILLS_ROOT}jq/SKILL.md", offset=100)

    res = asyncio.run(go())
    assert res.file_data is None
    assert res.error is not None
    assert "exceeds file length" in res.error


def test_template_aglob_no_match_returns_empty(fake_tm: _FakeResourceManager) -> None:
    """A glob that matches nothing returns an empty match list, not the backend's
    'No files found' sentinel string."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.aglob("*/NOPE.md")

    result = asyncio.run(go())
    assert result.matches == []


def test_template_aglob_star_is_non_recursive(fake_tm: _FakeResourceManager) -> None:
    """``*`` does NOT cross ``/``: ``*.md`` must not match a nested
    ``jq/SKILL.md`` (the wcmatch semantic that plain fnmatch would break)."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.aglob("*.md")

    result = asyncio.run(go())
    assert result.matches == []


def test_template_aglob_globstar_is_recursive(fake_tm: _FakeResourceManager) -> None:
    """``**`` crosses ``/`` (explicit recursion): ``**/*.md`` matches nested
    skill files."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        await fake_tm.upload_template("skills/flow/SKILL.md", "y")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.aglob("**/*.md")

    result = asyncio.run(go())
    assert {m["path"] for m in result.matches} == {"/jq/SKILL.md", "/flow/SKILL.md"}


def test_template_aglob_brace_expansion(fake_tm: _FakeResourceManager) -> None:
    """Brace alternation expands: ``{a,b}/SKILL.md`` matches both branches (the
    wcmatch semantic that plain fnmatch would not honor)."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/a/SKILL.md", "x")
        await fake_tm.upload_template("skills/b/SKILL.md", "y")
        await fake_tm.upload_template("skills/c/SKILL.md", "z")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.aglob("{a,b}/SKILL.md")

    result = asyncio.run(go())
    assert {m["path"] for m in result.matches} == {"/a/SKILL.md", "/b/SKILL.md"}


def test_template_aglob_leading_slash_pattern_matches(fake_tm: _FakeResourceManager) -> None:
    """A leading ``/`` on the pattern is stripped before matching, so
    ``/jq/SKILL.md`` matches the relative ``jq/SKILL.md``."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.aglob("/jq/SKILL.md")

    result = asyncio.run(go())
    assert {m["path"] for m in result.matches} == {"/jq/SKILL.md"}


def test_template_aglob_exact_file_base(fake_tm: _FakeResourceManager) -> None:
    """A ``path`` naming an exact skill file is the search base and matches on the
    bare filename (the exact-file base branch + its filename-relativisation arm),
    scoping the result to that one file."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        await fake_tm.upload_template("skills/flow/SKILL.md", "y")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.aglob("SKILL.md", path="/jq/SKILL.md")

    result = asyncio.run(go())
    assert {m["path"] for m in result.matches} == {"/jq/SKILL.md"}


def test_template_aglob_directory_base_with_and_without_trailing_slash(fake_tm: _FakeResourceManager) -> None:
    """A directory ``path`` scopes the search to that skill dir and matches relative
    to it (the directory-prefix filter + its ``<dir>/`` relativisation arm); a
    trailing slash is normalized away, so ``/jq`` and ``/jq/`` behave identically."""

    async def go() -> tuple[Any, Any]:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        await fake_tm.upload_template("skills/flow/SKILL.md", "y")
        skills = build_backend().routes[SKILLS_ROOT]
        no_slash = await skills.aglob("*.md", path="/jq")
        with_slash = await skills.aglob("*.md", path="/jq/")
        return no_slash, with_slash

    no_slash, with_slash = asyncio.run(go())
    # Only the /jq dir is in scope — /flow is filtered out by the directory prefix.
    assert {m["path"] for m in no_slash.matches} == {"/jq/SKILL.md"}
    assert {m["path"] for m in with_slash.matches} == {"/jq/SKILL.md"}


def test_template_aglob_whitespace_only_path_is_empty(fake_tm: _FakeResourceManager) -> None:
    """A whitespace-only ``path`` is invalid and yields no matches (the guard)."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.aglob("*", path="   ")

    result = asyncio.run(go())
    assert result.matches == []


def test_template_aglob_directory_base_no_match(fake_tm: _FakeResourceManager) -> None:
    """A directory ``path`` whose pattern matches nothing under it returns empty."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        skills = build_backend().routes[SKILLS_ROOT]
        return await skills.aglob("*.txt", path="/jq")

    result = asyncio.run(go())
    assert result.matches == []


def test_inline_aread_offset_beyond_inline_reports_error(fake_tm: _FakeResourceManager) -> None:
    """An offset past an inline skill's length surfaces the slice error too (the
    inline overlay's own read path, not the template delegate's)."""

    async def go() -> Any:
        backend = build_backend(inline_skills={"demo": "a\nb\n"})
        return await backend.aread(f"{SKILLS_ROOT}demo/SKILL.md", offset=100)

    res = asyncio.run(go())
    assert res.file_data is None
    assert res.error is not None
    assert "exceeds file length" in res.error
