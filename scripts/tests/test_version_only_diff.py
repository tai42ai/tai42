"""Unit tests for scripts/version_only_diff.py — the classifier ci.yml uses to
gate the heavy e2e / browser lanes off a diff that carries only release version
bumps. Hermetic: every case is a synthetic unified diff, so no git runs."""

from __future__ import annotations

from textwrap import dedent

import version_only_diff as vod  # importable via the scripts/ path conftest.py injects

_PYPROJECT_VERSION = dedent(
    """\
    diff --git a/core/kit/pyproject.toml b/core/kit/pyproject.toml
    --- a/core/kit/pyproject.toml
    +++ b/core/kit/pyproject.toml
    @@ -1,4 +1,4 @@
     [project]
     name = "tai42-kit"
    -version = "3.12.0"
    +version = "3.13.0"
    """
)

_PYPROJECT_DEP = dedent(
    """\
    diff --git a/core/kit/pyproject.toml b/core/kit/pyproject.toml
    --- a/core/kit/pyproject.toml
    +++ b/core/kit/pyproject.toml
    @@ -8,3 +8,3 @@
     dependencies = [
    -    "httpx>=0.27",
    +    "httpx>=0.28",
     ]
    """
)

_DESCRIPTOR_VERSION = dedent(
    """\
    diff --git a/plugins/agents/tai-plugin.yml b/plugins/agents/tai-plugin.yml
    --- a/plugins/agents/tai-plugin.yml
    +++ b/plugins/agents/tai-plugin.yml
    @@ -1,2 +1,2 @@
     name: agents
    -version: 5.5.3
    +version: 5.5.4
    """
)

_DESCRIPTOR_NON_VERSION = dedent(
    """\
    diff --git a/plugins/agents/tai-plugin.yml b/plugins/agents/tai-plugin.yml
    --- a/plugins/agents/tai-plugin.yml
    +++ b/plugins/agents/tai-plugin.yml
    @@ -3,2 +3,2 @@
    -summary: an agent
    +summary: an agent runtime
    """
)

_MANIFEST = dedent(
    """\
    diff --git a/.release-please-manifest.json b/.release-please-manifest.json
    --- a/.release-please-manifest.json
    +++ b/.release-please-manifest.json
    @@ -2,3 +2,3 @@
    -  "core/kit": "3.12.0",
    +  "core/kit": "3.13.0",
    """
)

_LOCK_FIRST_PARTY = dedent(
    """\
    diff --git a/uv.lock b/uv.lock
    --- a/uv.lock
    +++ b/uv.lock
    @@ -10,7 +10,7 @@
     [[package]]
     name = "tai42-kit"
    -version = "3.12.0"
    +version = "3.13.0"
     source = { editable = "core/kit" }
    """
)

_LOCK_THIRD_PARTY = dedent(
    """\
    diff --git a/uv.lock b/uv.lock
    --- a/uv.lock
    +++ b/uv.lock
    @@ -40,7 +40,7 @@
     [[package]]
     name = "httpx"
    -version = "0.27.0"
    +version = "0.28.0"
     source = { registry = "https://pypi.org/simple" }
    """
)

_LOCK_NON_VERSION = dedent(
    """\
    diff --git a/uv.lock b/uv.lock
    --- a/uv.lock
    +++ b/uv.lock
    @@ -12,3 +12,3 @@
     name = "tai42-kit"
     version = "3.13.0"
    -source = { editable = "core/kit" }
    +source = { editable = "core/kit-renamed" }
    """
)

_SOURCE = dedent(
    """\
    diff --git a/core/kit/src/tai42_kit/app.py b/core/kit/src/tai42_kit/app.py
    --- a/core/kit/src/tai42_kit/app.py
    +++ b/core/kit/src/tai42_kit/app.py
    @@ -1,2 +1,2 @@
    -x = 1
    +x = 2
    """
)


def test_empty_diff_is_not_version_only():
    assert vod.is_version_only({}) is False


def test_pyproject_version_bump_is_version_only():
    assert vod.is_version_only({"core/kit/pyproject.toml": _PYPROJECT_VERSION}) is True


def test_pyproject_dependency_change_is_not_version_only():
    assert vod.is_version_only({"core/kit/pyproject.toml": _PYPROJECT_DEP}) is False


def test_descriptor_version_bump_is_version_only():
    assert vod.is_version_only({"plugins/agents/tai-plugin.yml": _DESCRIPTOR_VERSION}) is True


def test_descriptor_non_version_change_is_not_version_only():
    assert vod.is_version_only({"plugins/agents/tai-plugin.yml": _DESCRIPTOR_NON_VERSION}) is False


def test_manifest_bump_is_version_only():
    assert vod.is_version_only({vod.MANIFEST: _MANIFEST}) is True


def test_lock_first_party_version_bump_is_version_only():
    assert vod.is_version_only({vod.LOCKFILE: _LOCK_FIRST_PARTY}) is True


def test_lock_third_party_version_bump_is_not_version_only():
    assert vod.is_version_only({vod.LOCKFILE: _LOCK_THIRD_PARTY}) is False


def test_lock_non_version_change_is_not_version_only():
    assert vod.is_version_only({vod.LOCKFILE: _LOCK_NON_VERSION}) is False


def test_source_change_is_not_version_only():
    assert vod.is_version_only({"core/kit/src/tai42_kit/app.py": _SOURCE}) is False


def test_a_release_train_merge_shape_is_version_only():
    # The exact shape of a train's merge onto main: member version lines, the
    # descriptor version, the manifest, and the first-party lock entries.
    assert (
        vod.is_version_only(
            {
                "core/kit/pyproject.toml": _PYPROJECT_VERSION,
                "plugins/agents/tai-plugin.yml": _DESCRIPTOR_VERSION,
                vod.MANIFEST: _MANIFEST,
                vod.LOCKFILE: _LOCK_FIRST_PARTY,
            }
        )
        is True
    )


def test_one_source_file_defeats_the_bump_set():
    assert (
        vod.is_version_only(
            {
                "core/kit/pyproject.toml": _PYPROJECT_VERSION,
                "core/kit/src/tai42_kit/app.py": _SOURCE,
            }
        )
        is False
    )
