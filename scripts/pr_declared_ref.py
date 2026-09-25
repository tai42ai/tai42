#!/usr/bin/env python3
"""Resolve the cross-repo ref a pull request declares in its body.

A change that spans this monorepo and a paired front-end repo (tai-studio) has
to be tested tree-against-tree, so a pull request may name the paired repo's ref
to check out for its browser lane. The author writes one line in the pull
request body::

    tai-studio-ref: my-feature-branch

The line is a plain ``<field>: <value>`` pair, not a Conventional-Commits header
(its key is not a release type), so it never begins a release-please bump chunk
and never leaks into the squash message's version projection. The value is a git
ref — a branch name or a full commit sha.

The field is read from the ``PR_BODY`` environment variable (never interpolated
into a shell). The first line whose key matches ``--field`` wins; its value is
the resolved ref, printed to stdout. When the field is absent — or the body is
empty, as on a push event — the default ``main`` is printed. A field that is
present but carries a value outside the accepted grammar fails loudly, naming the
field and the offending value, rather than silently falling back to ``main`` and
testing against the wrong tree.

The accepted grammar is a **branch name** or a **40-character commit sha**, and
nothing else. A branch name is one or more ``/``-separated segments, each starting
with a letter, digit or underscore and continuing with those plus ``.-`` (so
``area/topic`` passes); it may not begin with ``-``, contain ``..``, or carry a
``refs/`` prefix. Forbidding a fully-qualified ref (``refs/heads/x``,
``refs/pull/N/head``) keeps a declared ref from aiming a secret-bearing lane at an
unreviewed fork pull-request tree in the paired repo.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

DEFAULT_REF = "main"

# A 40-character commit sha.
_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
# A branch name: ``/``-separated segments, each starting with a letter, digit or
# underscore and continuing with those plus ``.-``. It rejects whitespace, shell
# metacharacters, a leading ``-``, ``.`` or ``/``, an empty segment, and (with the
# checks in :func:`resolve_ref`) the ``..`` sequence and a ``refs/`` prefix.
_BRANCH = re.compile(r"^[0-9A-Za-z_][0-9A-Za-z_.-]*(?:/[0-9A-Za-z_][0-9A-Za-z_.-]*)*$")


def _is_valid_ref(value: str) -> bool:
    """Whether ``value`` is an accepted ref — a 40-hex sha or a bare branch name."""
    if _SHA.match(value):
        return True
    return _BRANCH.match(value) is not None and ".." not in value and not value.startswith("refs/")


def resolve_ref(field: str, body: str) -> str:
    """The ref the body declares for ``field``, or :data:`DEFAULT_REF` when it declares none.

    The first line of the form ``<field>: <value>`` sets the ref; its value is
    stripped of surrounding whitespace. A line with the key but no non-space
    value counts as no declaration (the default applies). A declared value
    outside the accepted grammar raises :class:`ValueError`.
    """
    prefix = f"{field}:"
    for line in body.splitlines():
        if not line.startswith(prefix):
            continue
        value = line[len(prefix) :].strip()
        if not value:
            return DEFAULT_REF
        if not _is_valid_ref(value):
            raise ValueError(f"{field} declares a value that is not a branch name or 40-hex sha: {value!r}")
        return value
    return DEFAULT_REF


def main() -> int:
    """Print the resolved ref for ``--field``, or exit non-zero when the body declares a malformed one."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--field", required=True, help="the pull-request-body key naming the ref, e.g. tai-studio-ref")
    args = parser.parse_args()
    try:
        print(resolve_ref(args.field, os.environ.get("PR_BODY", "")))
    except ValueError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
