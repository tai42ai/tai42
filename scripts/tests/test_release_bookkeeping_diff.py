"""Unit tests for scripts/release_bookkeeping_diff.py — the classifier ci.yml uses to
gate the heavy e2e / browser lanes off a diff that carries only release bookkeeping,
and the consumer boot gate uses to tell a range-only release from one with its own
changes. Hermetic: every case is a synthetic unified diff, so no git runs."""

from __future__ import annotations

from textwrap import dedent

import release_bookkeeping_diff as rbd  # importable via the scripts/ path conftest.py injects

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


def test_empty_diff_is_not_bookkeeping():
    assert rbd.is_release_bookkeeping({}) is False


def test_pyproject_version_bump_is_release_bookkeeping():
    assert rbd.is_release_bookkeeping({"core/kit/pyproject.toml": _PYPROJECT_VERSION}) is True


def test_pyproject_dependency_change_is_not_bookkeeping():
    assert rbd.is_release_bookkeeping({"core/kit/pyproject.toml": _PYPROJECT_DEP}) is False


def test_descriptor_version_bump_is_release_bookkeeping():
    assert rbd.is_release_bookkeeping({"plugins/agents/tai-plugin.yml": _DESCRIPTOR_VERSION}) is True


def test_descriptor_non_version_change_is_not_bookkeeping():
    assert rbd.is_release_bookkeeping({"plugins/agents/tai-plugin.yml": _DESCRIPTOR_NON_VERSION}) is False


def test_manifest_bump_is_release_bookkeeping():
    assert rbd.is_release_bookkeeping({rbd.MANIFEST: _MANIFEST}) is True


def test_lock_first_party_version_bump_is_release_bookkeeping():
    assert rbd.is_release_bookkeeping({rbd.LOCKFILE: _LOCK_FIRST_PARTY}) is True


def test_lock_third_party_version_bump_is_not_bookkeeping():
    assert rbd.is_release_bookkeeping({rbd.LOCKFILE: _LOCK_THIRD_PARTY}) is False


def test_lock_non_version_change_is_not_bookkeeping():
    assert rbd.is_release_bookkeeping({rbd.LOCKFILE: _LOCK_NON_VERSION}) is False


def test_source_change_is_not_bookkeeping():
    assert rbd.is_release_bookkeeping({"core/kit/src/tai42_kit/app.py": _SOURCE}) is False


def test_a_release_train_merge_shape_is_release_bookkeeping():
    # The exact shape of a train's merge onto main: member version lines, the
    # descriptor version, the manifest, and the first-party lock entries.
    assert (
        rbd.is_release_bookkeeping(
            {
                "core/kit/pyproject.toml": _PYPROJECT_VERSION,
                "plugins/agents/tai-plugin.yml": _DESCRIPTOR_VERSION,
                rbd.MANIFEST: _MANIFEST,
                rbd.LOCKFILE: _LOCK_FIRST_PARTY,
            }
        )
        is True
    )


def test_one_source_file_defeats_the_bump_set():
    assert (
        rbd.is_release_bookkeeping(
            {
                "core/kit/pyproject.toml": _PYPROJECT_VERSION,
                "core/kit/src/tai42_kit/app.py": _SOURCE,
            }
        )
        is False
    )


def _pyproject_diff(*changed: str) -> str:
    body = "\n".join(changed)
    return (
        "diff --git a/plugins/demo/pyproject.toml b/plugins/demo/pyproject.toml\n"
        "--- a/plugins/demo/pyproject.toml\n"
        "+++ b/plugins/demo/pyproject.toml\n"
        "@@ -1,9 +1,9 @@\n"
        " dependencies = [\n"
        f"{body}\n"
        " ]\n"
    )


def _descriptor_diff(*changed: str) -> str:
    return (
        "diff --git a/plugins/demo/tai-plugin.yml b/plugins/demo/tai-plugin.yml\n"
        "--- a/plugins/demo/tai-plugin.yml\n"
        "+++ b/plugins/demo/tai-plugin.yml\n"
        "@@ -1,3 +1,3 @@\n"
        " package: tai42-demo\n" + "\n".join(changed) + "\n"
    )


def test_first_party_range_rewrites_are_bookkeeping():
    diff = _pyproject_diff(
        '-version = "1.0.4"',
        '+version = "1.0.5"',
        '-    "tai42-contract>=2.3,<3",',
        '-    "tai42-kit[postgres]>=5.1,<6",',
        '+    "tai42-contract>=3.0,<4",',
        '+    "tai42-kit[postgres]>=6.0,<7",',
    )
    assert rbd.is_release_bookkeeping({"plugins/demo/pyproject.toml": diff}) is True


def test_a_first_party_requirement_added_is_not_bookkeeping():
    diff = _pyproject_diff('+    "tai42-kit>=5.1,<6",')
    assert rbd.is_release_bookkeeping({"plugins/demo/pyproject.toml": diff}) is False


def test_a_first_party_requirement_swapped_for_another_is_not_bookkeeping():
    diff = _pyproject_diff('-    "tai42-kit>=5.1,<6",', '+    "tai42-skeleton>=5.1,<6",')
    assert rbd.is_release_bookkeeping({"plugins/demo/pyproject.toml": diff}) is False


def test_a_first_party_marker_change_is_not_bookkeeping():
    diff = _pyproject_diff(
        '-    "tai42-kit>=5.1,<6",',
        "+    \"tai42-kit>=5.1,<6; python_version >= '3.13'\",",
    )
    assert rbd.is_release_bookkeeping({"plugins/demo/pyproject.toml": diff}) is False


def test_a_third_party_range_change_is_not_bookkeeping():
    diff = _pyproject_diff('-    "httpx>=0.27",', '+    "httpx>=0.28",')
    assert rbd.is_release_bookkeeping({"plugins/demo/pyproject.toml": diff}) is False


def test_descriptor_contract_pin_rewrite_is_bookkeeping():
    diff = _descriptor_diff("-version: 1.0.4", "+version: 1.0.5", "-contract: '>=2.3,<3'", "+contract: '>=3.0,<4'")
    assert rbd.is_release_bookkeeping({"plugins/demo/tai-plugin.yml": diff}) is True


def test_descriptor_contract_pin_dropped_is_not_bookkeeping():
    diff = _descriptor_diff("-contract: '>=2.3,<3'")
    assert rbd.is_release_bookkeeping({"plugins/demo/tai-plugin.yml": diff}) is False


def test_a_simple_release_version_file_is_bookkeeping():
    diff = (
        "diff --git a/plugins/demo/version.txt b/plugins/demo/version.txt\n"
        "--- a/plugins/demo/version.txt\n"
        "+++ b/plugins/demo/version.txt\n"
        "@@ -1 +1 @@\n"
        "-2.3.4\n"
        "+2.3.5\n"
    )
    assert rbd.is_release_bookkeeping({"plugins/demo/version.txt": diff}) is True


def test_a_version_file_with_other_content_is_not_bookkeeping():
    diff = "--- a/plugins/demo/version.txt\n+++ b/plugins/demo/version.txt\n@@ -1 +1 @@\n-2.3.4\n+next\n"
    assert rbd.is_release_bookkeeping({"plugins/demo/version.txt": diff}) is False
