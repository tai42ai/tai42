"""Root fleet-consistency gate — tai42 is flow-agnostic: no downstream repo name and
no hardcoded downstream tool-name ever appears in a tracked tai42 source file. A hit is
a hard failure naming file:line:term.

The platform carries one generic park protocol shared by agents and tools; a parking tool
(a flow among them) is just a tool. tai42 must therefore name NO specific downstream and
NO specific parking-tool identifier — everything stays generic. This gate pins that.

The banned needles are assembled from fragments so this guard file satisfies the very
invariant it enforces (its own source must not contain the substrings). The ``.claude``
working tree is a separate checkout and is excluded — this gate audits tai42's own tree
only. The WhatsApp ``Flow`` interactive-message type is a third-party API concept, not a
parking tool, so a bare ``flow`` is deliberately NOT banned; the banned needles target the
downstream-specific identifiers only."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (needle, case_insensitive). Assembled from fragments so this file stays self-exempt.
_BANNED: tuple[tuple[str, bool], ...] = (
    ("babel" + "fish", True),  # the private downstream repo name — never in tai42 source
    ("flow" + "_graph", True),  # a downstream parking-tool's signature argument identifier
    ('base_tool="' + 'flow"', False),  # the downstream flow preset example
    ("base_tool='" + "flow'", False),
)


def _scanned_files() -> list[str]:
    # Tracked files unioned with untracked-but-not-ignored ones (so a hit in a NEW file is
    # caught before git-add), minus the ``.claude`` working tree (a separate checkout).
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True, text=False).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=False,
    ).stdout
    rels = [p.decode("utf-8") for p in (tracked + untracked).split(b"\x00") if p]
    return [rel for rel in rels if not rel.startswith(".claude/") and rel != "uv.lock"]


def _scan_hits() -> list[str]:
    hits: list[str] = []
    for rel in _scanned_files():
        path = ROOT / rel
        if not path.is_file():  # tracked but deleted in the worktree
            continue
        data = path.read_bytes()
        if b"\x00" in data:  # binary: the text-term invariant does not apply
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for needle, ci in _BANNED:
                haystack = line.lower() if ci else line
                target = needle.lower() if ci else needle
                if target in haystack:
                    hits.append(f"{rel}:{lineno}:{needle}")
    return hits


def test_no_downstream_or_flow_tool_names():
    hits = _scan_hits()
    assert not hits, "downstream / flow-tool-name reference(s) found in tai42 source:\n" + "\n".join(hits)


def test_scan_is_not_vacuous():
    # A green result must come from reading the whole tree, never an empty scan set.
    assert len(_scanned_files()) > 500
