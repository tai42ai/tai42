"""``langchain_deep_agent`` multi-file skills: whole-directory listing/read/glob/grep
visibility, skill-dir detection, middleware discovery, and write refusals.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tests._langchain_deep_agent_backend_support import (
    _FakeResourceManager,
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


_MULTI_FILE_SKILL_MD = """---
name: jq
description: Query and transform JSON documents with the jq language.
---

# JQ Skill

See `references/cheatsheet.md` and run `scripts/run.sh`.
"""


async def _seed_multi_file_skill(fake_tm: _FakeResourceManager) -> None:
    """Seed a bundle: SKILL.md + a references/ file + a scripts/ file."""
    await fake_tm.upload_template("skills/jq/SKILL.md", _MULTI_FILE_SKILL_MD)
    await fake_tm.upload_template("skills/jq/references/cheatsheet.md", "pipe with |")
    await fake_tm.upload_template("skills/jq/scripts/run.sh", "#!/bin/sh\njq .\n")


def test_multi_file_skill_ls_read_glob_grep_see_all_files(fake_tm: _FakeResourceManager) -> None:
    """A skill bundle's reference files and scripts are served, not only SKILL.md:
    ``als`` lists the subdirs, a nested read returns raw content, ``aglob`` finds
    every file, and ``agrep`` searches the bundled files' bodies."""

    async def go() -> tuple[Any, Any, Any, Any, Any]:
        await _seed_multi_file_skill(fake_tm)
        skills = build_backend().routes[SKILLS_ROOT]
        top = await skills.als("/jq/")
        refs = await skills.als("/jq/references/")
        cheatsheet = await skills.aread("/jq/references/cheatsheet.md")
        globbed = await skills.aglob("**/*")
        grepped = await skills.agrep("pipe with")
        return top, refs, cheatsheet, globbed, grepped

    top, refs, cheatsheet, globbed, grepped = asyncio.run(go())
    # Listing the skill dir shows SKILL.md plus the two bundle subdirs, and the
    # skill dir's own is_dir self entry (middleware discovery).
    top_paths = {e["path"] for e in top.entries}
    assert "/jq/" in {e["path"] for e in top.entries if e["is_dir"]}
    assert "/jq/SKILL.md" in {e["path"] for e in top.entries if not e["is_dir"]}
    assert {"/jq/references/", "/jq/scripts/"} <= {e["path"] for e in top.entries if e["is_dir"]}
    assert "/jq/references/cheatsheet.md" not in top_paths  # non-recursive: only direct children
    # A subdir lists its file child.
    assert [e["path"] for e in refs.entries] == ["/jq/references/cheatsheet.md"]
    # A nested reference file reads back raw.
    assert cheatsheet.error is None
    assert cheatsheet.file_data["content"] == "pipe with |"
    # Glob and grep reach every bundled file.
    assert {m["path"] for m in globbed.matches} == {
        "/jq/SKILL.md",
        "/jq/references/cheatsheet.md",
        "/jq/scripts/run.sh",
    }
    assert {m["path"] for m in grepped.matches} == {"/jq/references/cheatsheet.md"}


def test_dir_without_skill_md_is_not_a_skill_but_files_are_listed(fake_tm: _FakeResourceManager) -> None:
    """A directory under the mount that lacks SKILL.md is NOT a skill: it emits no
    skill-directory self entry. Its files are still served (never silently dropped
    — the loudness law), so a caller can list and read them under their real path."""

    async def go() -> tuple[Any, Any, Any]:
        await fake_tm.upload_template("skills/orphan/data.txt", "raw bytes")
        skills = build_backend().routes[SKILLS_ROOT]
        listed = await skills.als("/orphan/")
        content = await skills.aread("/orphan/data.txt")
        root = await skills.als("/")
        return listed, content, root

    listed, content, root = asyncio.run(go())
    # No SKILL.md -> no self entry for the dir (it is not a skill).
    assert "/orphan/" not in {e["path"] for e in listed.entries if e["is_dir"]}
    # But its file is listed and readable.
    assert [e["path"] for e in listed.entries] == ["/orphan/data.txt"]
    assert content.error is None
    assert content.file_data["content"] == "raw bytes"
    # It still appears as a plain directory when listing the mount root.
    assert "/orphan/" in {e["path"] for e in root.entries if e["is_dir"]}


def test_middleware_discovers_multi_file_skill_via_per_skill_source(fake_tm: _FakeResourceManager) -> None:
    """Empirical reconciliation check against deepagents 0.7.5's SkillsMiddleware:
    its ``_alist_skills`` enumerates the ``is_dir`` entries ``als`` returns for a
    source and downloads ``<dir>/SKILL.md`` from each. The per-skill source
    ``/skills/jq/`` (and the mount-root source ``/skills/``) must resolve to
    exactly the ``jq`` skill — the widening's extra bundle subdirs (references/,
    scripts/) carry no SKILL.md and are harmlessly skipped, never dropping jq."""
    from deepagents.middleware.skills import _alist_skills

    async def go() -> tuple[Any, Any]:
        await _seed_multi_file_skill(fake_tm)
        backend = build_backend()
        per_skill = await _alist_skills(backend, f"{SKILLS_ROOT}jq/")
        whole_mount = await _alist_skills(backend, SKILLS_ROOT)
        return per_skill, whole_mount

    per_skill, whole_mount = asyncio.run(go())
    assert [s["name"] for s in per_skill] == ["jq"]
    assert [s["path"] for s in per_skill] == [f"{SKILLS_ROOT}jq/SKILL.md"]
    assert [s["name"] for s in whole_mount] == ["jq"]


def test_multi_file_skill_writes_still_refuse(fake_tm: _FakeResourceManager) -> None:
    """The whole mount stays read-only: writing a bundled reference file refuses,
    just as writing SKILL.md does."""
    skills = TemplateSkillsBackend()

    async def go() -> None:
        with pytest.raises(PermissionError, match="read-only"):
            await skills.awrite("/jq/references/cheatsheet.md", "x")
        with pytest.raises(PermissionError, match="read-only"):
            await skills.aupload_files([("/jq/scripts/run.sh", b"x")])

    asyncio.run(go())
