"""``langchain_deep_agent`` inline-skill backend: routing, read-back, shadowing of
template skills, and the aglob/agrep/als operations plus write refusals.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tests._langchain_deep_agent_backend_support import (
    _FakeResourceManager,
    _TemplateMissing,
)

from tai42_agents.langchain_deep_agent.backend import (
    SKILLS_ROOT,
    InlineSkillsBackend,
    TemplateSkillsBackend,
    build_backend,
)


@pytest.fixture
def fake_tm(monkeypatch: pytest.MonkeyPatch) -> _FakeResourceManager:
    tm = _FakeResourceManager()
    monkeypatch.setattr(tai42_app.storage, "resource_manager", tm)
    return tm


def test_build_backend_routes_inline_skills_backend(fake_tm: _FakeResourceManager) -> None:
    backend = build_backend(inline_skills={"demo": "x"})
    assert isinstance(backend.routes[SKILLS_ROOT], InlineSkillsBackend)


def test_build_backend_empty_inline_skills_uses_template_backend(fake_tm: _FakeResourceManager) -> None:
    """An empty/None inline map keeps the plain template backend (no overlay)."""
    assert isinstance(build_backend(None).routes[SKILLS_ROOT], TemplateSkillsBackend)
    assert isinstance(build_backend({}).routes[SKILLS_ROOT], TemplateSkillsBackend)


def test_inline_skill_reads_back_through_backend(fake_tm: _FakeResourceManager) -> None:
    """An inline skill's content is served at its /skills/<name>/SKILL.md path."""

    async def go() -> Any:
        backend = build_backend(inline_skills={"demo": "INLINE BODY"})
        return await backend.aread(f"{SKILLS_ROOT}demo/SKILL.md")

    res = asyncio.run(go())
    assert res.error is None
    assert res.file_data["content"] == "INLINE BODY"


def test_inline_skill_body_is_not_jinja_rendered(fake_tm: _FakeResourceManager) -> None:
    async def go() -> Any:
        backend = build_backend(inline_skills={"demo": "use {{ raw }} here"})
        return await backend.aread(f"{SKILLS_ROOT}demo/SKILL.md")

    res = asyncio.run(go())
    assert res.file_data["content"] == "use {{ raw }} here"


def test_inline_and_template_skills_coexist(fake_tm: _FakeResourceManager) -> None:
    """Inline names overlay the template store; listings and reads cover both."""

    async def go() -> tuple[Any, Any, Any]:
        await fake_tm.upload_template("skills/jq/SKILL.md", "TEMPLATE JQ")
        backend = build_backend(inline_skills={"demo": "INLINE DEMO"})
        listed = await backend.als(SKILLS_ROOT)
        inline_read = await backend.aread(f"{SKILLS_ROOT}demo/SKILL.md")
        template_read = await backend.aread(f"{SKILLS_ROOT}jq/SKILL.md")
        return listed, inline_read, template_read

    listed, inline_read, template_read = asyncio.run(go())
    assert {e["path"] for e in listed.entries} == {f"{SKILLS_ROOT}jq/", f"{SKILLS_ROOT}demo/"}
    assert inline_read.file_data["content"] == "INLINE DEMO"
    assert template_read.file_data["content"] == "TEMPLATE JQ"


def test_inline_skill_shadows_template_skill_of_same_name(fake_tm: _FakeResourceManager) -> None:
    """When a name exists in both, the inline content wins (overlay)."""

    async def go() -> tuple[Any, Any]:
        await fake_tm.upload_template("skills/jq/SKILL.md", "FROM TEMPLATE")
        backend = build_backend(inline_skills={"jq": "FROM INLINE"})
        listed = await backend.als(SKILLS_ROOT)
        read = await backend.aread(f"{SKILLS_ROOT}jq/SKILL.md")
        return listed, read

    listed, read = asyncio.run(go())
    assert [e["path"] for e in listed.entries] == [f"{SKILLS_ROOT}jq/"]
    assert read.file_data["content"] == "FROM INLINE"


def test_inline_shadow_skips_vanished_template_fetch(fake_tm: _FakeResourceManager) -> None:
    """An inline skill shadowing a template skill whose key has vanished from the
    store must not fetch that template during ``agrep``: the inline body overrides
    it, so the vanished template is irrelevant and its absence must not raise. The
    shadow-skip runs before the fetch, so ``agrep`` returns the inline match."""

    async def go() -> Any:
        fake_tm.phantom_keys.add("skills/jq/SKILL.md")
        skills = build_backend(inline_skills={"jq": "INLINE"}).routes[SKILLS_ROOT]
        return await skills.agrep("INLINE")

    result = asyncio.run(go())
    assert {match["path"] for match in result.matches} == {"/jq/SKILL.md"}


def test_inline_download_files_serves_inline_and_delegates(fake_tm: _FakeResourceManager) -> None:
    """download_files returns inline content for inline names and delegates the rest."""

    async def go() -> list[Any]:
        await fake_tm.upload_template("skills/jq/SKILL.md", "TEMPLATE JQ")
        skills = build_backend(inline_skills={"demo": "INLINE DEMO"}).routes[SKILLS_ROOT]
        return await skills.adownload_files(["/demo/SKILL.md", "/jq/SKILL.md", "/missing/SKILL.md"])

    responses = asyncio.run(go())
    inline, template, missing = responses
    # Order is preserved one-to-one with the requested paths.
    assert [r.path for r in responses] == ["/demo/SKILL.md", "/jq/SKILL.md", "/missing/SKILL.md"]
    assert inline.content == b"INLINE DEMO"
    assert inline.error is None
    assert template.content == b"TEMPLATE JQ"
    assert template.error is None
    assert missing.content is None
    assert missing.error == "file_not_found"


def test_inline_aglob_merges_inline_and_template(fake_tm: _FakeResourceManager) -> None:
    """aglob returns the union of inline and template skill SKILL.md paths."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        skills = build_backend(inline_skills={"demo": "y"}).routes[SKILLS_ROOT]
        return await skills.aglob("*/SKILL.md")

    result = asyncio.run(go())
    assert {m["path"] for m in result.matches} == {"/jq/SKILL.md", "/demo/SKILL.md"}


def test_inline_aglob_exact_file_base(fake_tm: _FakeResourceManager) -> None:
    """The inline overlay honors a ``path`` naming an exact inline skill file (the
    exact-file base branch over the inline path set), scoping to that one file."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        skills = build_backend(inline_skills={"demo": "y"}).routes[SKILLS_ROOT]
        return await skills.aglob("SKILL.md", path="/demo/SKILL.md")

    result = asyncio.run(go())
    assert {m["path"] for m in result.matches} == {"/demo/SKILL.md"}


def test_inline_aglob_directory_base_with_and_without_trailing_slash(fake_tm: _FakeResourceManager) -> None:
    """The inline overlay scopes to a directory ``path`` and normalizes a trailing
    slash, so ``/demo`` and ``/demo/`` match the inline skill identically while an
    out-of-scope template skill is filtered out."""

    async def go() -> tuple[Any, Any]:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        skills = build_backend(inline_skills={"demo": "y"}).routes[SKILLS_ROOT]
        no_slash = await skills.aglob("*.md", path="/demo")
        with_slash = await skills.aglob("*.md", path="/demo/")
        return no_slash, with_slash

    no_slash, with_slash = asyncio.run(go())
    assert {m["path"] for m in no_slash.matches} == {"/demo/SKILL.md"}
    assert {m["path"] for m in with_slash.matches} == {"/demo/SKILL.md"}


def test_inline_aglob_whitespace_only_path_is_empty(fake_tm: _FakeResourceManager) -> None:
    """A whitespace-only ``path`` yields no matches on the inline overlay either —
    both the template delegate and the inline path set reject it."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/jq/SKILL.md", "x")
        skills = build_backend(inline_skills={"demo": "y"}).routes[SKILLS_ROOT]
        return await skills.aglob("*", path="   ")

    result = asyncio.run(go())
    assert result.matches == []


def test_inline_aglob_directory_base_no_match(fake_tm: _FakeResourceManager) -> None:
    """A directory ``path`` whose pattern matches nothing returns empty on the inline
    overlay."""

    async def go() -> Any:
        skills = build_backend(inline_skills={"demo": "y"}).routes[SKILLS_ROOT]
        return await skills.aglob("*.txt", path="/demo")

    result = asyncio.run(go())
    assert result.matches == []


def test_inline_agrep_shadows_template_and_includes_inline(fake_tm: _FakeResourceManager) -> None:
    """agrep searches inline content and the template skills it does not shadow;
    a template skill shadowed by an inline name of the same name is not searched."""

    async def go() -> tuple[Any, Any, Any]:
        await fake_tm.upload_template("skills/jq/SKILL.md", "TEMPLATE_MARKER")
        await fake_tm.upload_template("skills/keep/SKILL.md", "KEEP_MARKER")
        # 'jq' inline shadows the template 'jq'; its body carries no marker.
        skills = build_backend(inline_skills={"jq": "INLINE_MARKER"}).routes[SKILLS_ROOT]
        shadowed = await skills.agrep("TEMPLATE_MARKER")
        inline_hit = await skills.agrep("INLINE_MARKER")
        unshadowed = await skills.agrep("KEEP_MARKER")
        return shadowed, inline_hit, unshadowed

    shadowed, inline_hit, unshadowed = asyncio.run(go())
    assert shadowed.matches == []
    assert {m["path"] for m in inline_hit.matches} == {"/jq/SKILL.md"}
    assert {m["path"] for m in unshadowed.matches} == {"/keep/SKILL.md"}


def test_inline_agrep_propagates_vanished_listed_key(fake_tm: _FakeResourceManager) -> None:
    """A template key the provider lists but which is absent from the store
    propagates the provider's own error out of the inline overlay's agrep, rather
    than being silently dropped from the search corpus."""

    async def go() -> None:
        fake_tm.phantom_keys.add("skills/gone/SKILL.md")
        skills = build_backend(inline_skills={"demo": "INLINE"}).routes[SKILLS_ROOT]
        with pytest.raises(_TemplateMissing):
            await skills.agrep("anything")

    asyncio.run(go())


def test_inline_agrep_indexes_empty_body_template_skill(fake_tm: _FakeResourceManager) -> None:
    """An un-shadowed empty-string template body stays in the inline overlay's
    search corpus: grepping the empty pattern matches its one empty line, proving
    it was indexed (guards against an ``if content:`` truthiness check dropping
    it)."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/empty/SKILL.md", "")
        skills = build_backend(inline_skills={"demo": "INLINE"}).routes[SKILLS_ROOT]
        return await skills.agrep("")

    result = asyncio.run(go())
    assert "/empty/SKILL.md" in {m["path"] for m in result.matches}


def test_inline_als_skips_inline_dir_not_under_path(fake_tm: _FakeResourceManager) -> None:
    """An inline skill dir that is not under the requested listing base is excluded
    (the ``skill_dir.startswith(normalized)`` false leg): listing under one skill's
    dir shows that dir (and its children), never an out-of-scope inline skill."""

    async def go() -> Any:
        await fake_tm.upload_template("skills/other/SKILL.md", "x")
        backend = build_backend(inline_skills={"demo": "y"})
        return await backend.als(f"{SKILLS_ROOT}other/")

    result = asyncio.run(go())
    paths = {e["path"] for e in result.entries}
    # The out-of-scope inline skill is absent; the in-scope skill dir is present.
    assert f"{SKILLS_ROOT}demo/" not in paths
    assert f"{SKILLS_ROOT}other/" in {e["path"] for e in result.entries if e["is_dir"]}


def test_inline_skills_writes_raise(fake_tm: _FakeResourceManager) -> None:
    skills = InlineSkillsBackend({"demo": "x"})

    async def go() -> None:
        with pytest.raises(PermissionError, match="read-only"):
            await skills.awrite("/demo/SKILL.md", "x")
        with pytest.raises(PermissionError, match="read-only"):
            await skills.aedit("/demo/SKILL.md", "a", "b")
        with pytest.raises(PermissionError, match="read-only"):
            await skills.aupload_files([("/demo/SKILL.md", b"x")])

    asyncio.run(go())


def test_inline_sync_methods_raise_not_implemented(fake_tm: _FakeResourceManager) -> None:
    """The inline overlay is async-only too; every sync read entrypoint raises."""
    skills = InlineSkillsBackend({"demo": "x"})
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.read("/demo/SKILL.md")
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.ls("/")
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.glob("*")
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.grep("x")
    with pytest.raises(NotImplementedError, match="async-only"):
        skills.download_files(["/demo/SKILL.md"])
