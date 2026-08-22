"""``claude_code`` skills sync: whole-tree copy from the store, inline skills, and containment."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.app import tai42_app
from tests._claude_app import build_local_app

from tai42_agents.claude_code.skills_sync import SkillNameError, sync_skills, validate_name
from tai42_agents.claude_code.skills_sync import _dest_path as dest_path

_TEMPLATES = {
    "skills/jq/SKILL.md": "# jq skill",
    "skills/jq/references/guide.md": "reference body",
    "skills/jq/scripts/run.sh": "echo hi",
    "skills/other/SKILL.md": "# other skill",
}


class _RecordingSession:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def put_file(self, path: str, data: bytes) -> None:
        self.files[path] = data


def _run_sync(skill_names: list[str], inline: list[dict]) -> dict[str, bytes]:
    session = _RecordingSession()
    app = build_local_app(templates=dict(_TEMPLATES))
    with tai42_app.bound(app):
        asyncio.run(sync_skills(session, skill_names=skill_names, inline_skills=inline))  # type: ignore[arg-type]
    return session.files


def test_copies_the_whole_skill_tree_including_scripts_and_references() -> None:
    files = _run_sync(["jq"], [])
    assert files["project/.claude/skills/jq/SKILL.md"] == b"# jq skill"
    assert files["project/.claude/skills/jq/references/guide.md"] == b"reference body"
    assert files["project/.claude/skills/jq/scripts/run.sh"] == b"echo hi"
    # A non-requested skill is never copied.
    assert not any("other" in path for path in files)


def test_inline_skill_becomes_a_skill_md() -> None:
    files = _run_sync([], [{"name": "inline1", "content": "# inline body"}])
    assert files["project/.claude/skills/inline1/SKILL.md"] == b"# inline body"


def test_unknown_skill_raises() -> None:
    with pytest.raises(ValueError, match="no files"):
        _run_sync(["missing"], [])


def test_invalid_skill_name_raises() -> None:
    with pytest.raises(SkillNameError):
        _run_sync(["../evil"], [])


def test_invalid_inline_name_raises() -> None:
    with pytest.raises(SkillNameError):
        _run_sync([], [{"name": "bad/name", "content": ""}])


def test_dest_path_rejects_traversal() -> None:
    with pytest.raises(ValueError, match="escapes its subtree"):
        dest_path("jq", "../../etc/passwd")


def test_validate_name_accepts_charset() -> None:
    assert validate_name("skill", "my-skill_1") == "my-skill_1"
