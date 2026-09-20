#!/usr/bin/env python3
"""Fail when a workspace dependency floor is only satisfiable by an upstream PRE-RELEASE.

The failure mode: a plugin pins a dependency floor (e.g. ``pydantic-core>=2.48.0``) that only
an upstream pre-release satisfies (``pydantic 2.14.0b1``). uv's default resolution strategy
(``if-necessary-or-explicit`` — pre-releases permitted when a specifier demands them) locks the
beta and stays green, while a stable-only image install does a fresh, pre-release-free resolve
and hits ResolutionImpossible.

The guard reproduces the stable-only resolve:

  * ``--upgrade`` so the committed ``uv.lock``'s pins never act as preferences that
    mask the floor (a leftover beta pin would otherwise resolve green);
  * ``--dry-run`` so ``uv.lock`` is never written — the repo tree is not mutated;
  * ``--prerelease if-necessary`` — uv's ``if-necessary`` is package-GLOBAL: a
    pre-release is admitted only for a package whose every published version is a
    pre-release (e.g. ``opentelemetry-semantic-conventions``, which ships only
    ``0.NNbM`` builds and is legitimately allowed). A package that HAS stable
    releases but whose requested range excludes all of them (``pydantic``, forced
    to a beta by the core floor) is refused and resolution fails naming it — the
    exact class a stable-only downstream install cannot satisfy.

Resolution only; nothing is installed. When the strict resolve fails, the guard
re-resolves with ``--prerelease allow`` to confirm the barrier was the disallowed
pre-release (allow succeeds) rather than an unrelated version conflict (allow also
fails), and reports accordingly with uv's own diagnostics.

Standard library only; the resolution work is delegated to the pinned ``uv``.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def resolve(prerelease: str) -> subprocess.CompletedProcess[str]:
    """Fresh, non-mutating workspace resolve under the given pre-release strategy."""
    return subprocess.run(  # noqa: S603 fixed, trusted argv; no shell and no user input
        ["uv", "lock", "--upgrade", "--prerelease", prerelease, "--dry-run"],  # noqa: S607 fixed, trusted executable resolved from PATH
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    """Run the stable-only resolve and return an exit code (1 on a pre-release-only floor)."""
    strict = resolve("if-necessary")
    if strict.returncode == 0:
        print("stable-resolution: OK — every workspace floor resolves without a pre-release.")
        return 0

    # The stable-only resolve failed. Re-resolve allowing pre-releases to learn
    # whether the disallowed pre-release was the sole barrier.
    loose = resolve("allow")
    sys.stderr.write(strict.stderr)

    if loose.returncode == 0:
        sys.stderr.write(
            "\n::error::stable-resolution: a workspace dependency floor is only "
            "satisfiable by a pre-release upstream. The resolve above fails under the "
            "stable-only `if-necessary` strategy but succeeds with `--prerelease=allow`, "
            "so the named package has stable releases none of which satisfy the requested "
            "range. A stable-only downstream image install would hit ResolutionImpossible "
            "on this. Lower the offending floor to a released "
            "stable version.\n"
        )
        return 1

    sys.stderr.write(
        "\n::error::stable-resolution: the workspace does not resolve even with "
        "pre-releases enabled (`--prerelease=allow` also failed), so this is an unrelated "
        "resolution conflict rather than a pre-release-only floor. See uv's report above.\n"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
