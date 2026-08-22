"""Materialize the requested skills into a session's ``project/.claude/skills/`` tree.

For each requested (charset-validated) skill NAME the whole ``skills/<name>/`` tree is copied
DIRECTLY from the template store (``tai42_app.storage.resource_manager``) into the
workspace-relative ``project/.claude/skills/<name>/`` — every key is already listable there, so
this does not depend on the skills-backend widening. Inline skills become a single ``SKILL.md``.

Every caller-supplied NAME used in a path is validated to the workspace-key charset
(``[A-Za-z0-9_-]{1,64}``) at the door, and every write stays realpath-contained to its subtree
(the session ``put_file`` roots relative paths at ``workspace_path`` and rejects an escape).
"""

from __future__ import annotations

import posixpath
import re
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.sandbox import SandboxSession

# Caller-supplied names (skill / inline-skill / subagent) used in a path are bounded to the
# same charset as a workspace key — no separators, no traversal.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The store key prefix and the workspace-relative destination the skills tree lands under.
_STORE_SKILLS_PREFIX = "skills/"
_SKILLS_DEST_ROOT = "project/.claude/skills"
_SKILL_FILENAME = "SKILL.md"


class SkillNameError(ValueError):
    """A caller-supplied skill/subagent name is outside the safe path charset. Raised loudly at
    the door so an unauthenticated name can never widen a materialization path."""

    def __init__(self, kind: str, name: str) -> None:
        self.kind = kind
        self.name = name
        super().__init__(f"{kind} name {name!r} is not a valid identifier ({_NAME_RE.pattern})")


def validate_name(kind: str, name: str) -> str:
    """Return ``name`` if it matches the safe charset, else raise :class:`SkillNameError`."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise SkillNameError(kind, name)
    return name


def _dest_path(name: str, subpath: str) -> str:
    """The workspace-relative destination for one skill file, guarded against traversal.

    ``subpath`` is the key remainder after ``skills/<name>/``; a normalized path that escapes
    the skill's own subtree raises rather than writing outside it.
    """
    base = f"{_SKILLS_DEST_ROOT}/{name}"
    dest = posixpath.normpath(f"{base}/{subpath}")
    if dest != base and not dest.startswith(base + "/"):
        raise ValueError(f"skill {name!r} file {subpath!r} escapes its subtree")
    return dest


async def sync_skills(
    session: SandboxSession,
    *,
    skill_names: list[str],
    inline_skills: list[dict[str, Any]],
) -> None:
    """Copy every requested skill tree and write every inline skill into the session workspace.

    Idempotent: each turn re-authors the same files. Raises loudly on an invalid name or a
    store key that escapes its skill subtree — never a silent skip.
    """
    if skill_names:
        keys = await tai42_app.storage.resource_manager.list_resources()
        for name in skill_names:
            validate_name("skill", name)
            prefix = f"{_STORE_SKILLS_PREFIX}{name}/"
            matched = [key for key in keys if key.startswith(prefix)]
            if not matched:
                raise ValueError(f"skill {name!r} has no files under {prefix!r} in the template store")
            for key in matched:
                subpath = key[len(prefix) :]
                content = await tai42_app.storage.resource_manager.fetch_template(key)
                await session.put_file(_dest_path(name, subpath), content.encode("utf-8"))

    for skill in inline_skills:
        name = validate_name("inline skill", skill["name"])
        content = skill.get("content", "")
        await session.put_file(_dest_path(name, _SKILL_FILENAME), content.encode("utf-8"))
