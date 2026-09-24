"""Unit tests for scripts/release_label_check.py — the projection the pre-merge gate runs on.

Hermetic: every case drives the pure projection helpers with synthetic titles,
bodies, changed-file lists and release-please config/manifest fragments. The
``tai42-api-gate`` subprocess the orchestration runs is not exercised here — the
gate has its own suite; these tests pin the title/body -> bump -> version and
file -> package attribution that decide which gate calls happen and at what
version.
"""

from __future__ import annotations

import release_label_check as rlc  # importable via the scripts/ path conftest.py injects
from release_label_check import Bump


def _packaged(root, *dirs: str) -> None:
    """Give each dir a pyproject.toml under ``root`` so the check treats it as a packaged member."""
    for directory in dirs:
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory / "pyproject.toml").write_text("[project]\n")


def test_title_only_fix_is_a_patch() -> None:
    assert rlc.projected_bump("fix(cli): correct a typo", "") == Bump.PATCH


def test_title_only_feat_is_a_minor() -> None:
    assert rlc.projected_bump("feat(kit): add a helper", "") == Bump.MINOR


def test_non_releasing_type_projects_nothing() -> None:
    assert rlc.projected_bump("chore: bump a dev dependency", "") == Bump.NONE
    assert rlc.projected_bump("docs: expand the readme", "") == Bump.NONE


def test_blank_line_then_whitelisted_header_starts_a_chunk() -> None:
    # A patch title with a plain feat chunk a blank line below projects the higher minor:
    # the body chunk begins at a whitelisted "type(scope): " header, so release-please splits it.
    assert rlc.projected_bump("fix(kit): a fix", "feat(kit): and also a feature") == Bump.MINOR


def test_bang_on_the_header_is_a_major() -> None:
    assert rlc.projected_bump("feat(kit)!: drop a public symbol", "") == Bump.MAJOR
    assert rlc.projected_bump("fix!: a breaking fix", "") == Bump.MAJOR


def test_bang_on_a_body_line_is_not_a_major() -> None:
    # release-please's chunk split excludes the "!" marker, so a "type!:" body line
    # starts no chunk and stays plain body text: the title's fix keeps it a patch.
    assert rlc.projected_bump("fix(kit): tidy", "refactor(kit)!: remove a method") == Bump.PATCH


def test_bang_line_after_a_blank_line_still_starts_no_chunk() -> None:
    # Even preceded by a blank line, "feat(kit)!:" is not in the split lookahead (the "!"),
    # so it never becomes a chunk header and never lifts the bump above the title's patch.
    assert rlc.projected_bump("fix: a fix", "\nfeat(kit)!: add a thing") == Bump.PATCH


def test_bang_line_after_a_non_whitelisted_line_stays_plain_body() -> None:
    # A "feat!:" line that follows a non-whitelisted body line (no blank-line + whitelisted
    # header before it) is plain body text: the title's fix decides — a patch.
    assert rlc.projected_bump("fix: a fix", "some free-form context\nfeat!: add") == Bump.PATCH


def test_breaking_change_footer_in_a_chunk_is_a_major() -> None:
    assert rlc.projected_bump("fix(kit): tidy up", "BREAKING CHANGE: removed the deprecated router") == Bump.MAJOR


def test_breaking_change_hyphen_footer_is_a_major() -> None:
    assert rlc.projected_bump("feat: add", "BREAKING-CHANGE: the shape changed") == Bump.MAJOR


def test_commit_override_replaces_the_whole_message_down_to_patch() -> None:
    # A BEGIN_COMMIT_OVERRIDE block replaces the title too: a feat! title is discarded.
    body = "BEGIN_COMMIT_OVERRIDE\nfix: tidy\nEND_COMMIT_OVERRIDE"
    assert rlc.projected_bump("feat!: rewrite everything", body) == Bump.PATCH


def test_commit_override_replaces_the_whole_message_up_to_major() -> None:
    body = "BEGIN_COMMIT_OVERRIDE\nfeat!: a breaking change\nEND_COMMIT_OVERRIDE"
    assert rlc.projected_bump("fix: tidy", body) == Bump.MAJOR


def test_commit_override_carrying_a_breaking_footer_is_a_major() -> None:
    body = "BEGIN_COMMIT_OVERRIDE\nfix: tidy\n\nBREAKING CHANGE: it moved\nEND_COMMIT_OVERRIDE"
    assert rlc.projected_bump("fix: tidy", body) == Bump.MAJOR


def test_nested_commit_block_contributes_its_own_bump() -> None:
    # A nested feat! block beside a fix: title lifts the projection to a major.
    body = "some context\n\nBEGIN_NESTED_COMMIT\nfeat!: a nested breaking change\nEND_NESTED_COMMIT"
    assert rlc.projected_bump("fix: tidy", body) == Bump.MAJOR


def test_nested_commit_block_of_plain_text_adds_no_bump() -> None:
    body = "BEGIN_NESTED_COMMIT\njust some prose, no conventional header\nEND_NESTED_COMMIT"
    assert rlc.projected_bump("chore: housekeeping", body) == Bump.NONE


def test_commit_override_stops_at_the_second_begin_marker() -> None:
    # release-please reads the slice between the FIRST and SECOND BEGIN markers, then up to
    # the first END; a breaking footer after the second BEGIN is outside the override text.
    body = "BEGIN_COMMIT_OVERRIDE\nfix: tidy\nBEGIN_COMMIT_OVERRIDE\n\nBREAKING CHANGE: gone\nEND_COMMIT_OVERRIDE"
    assert rlc.projected_bump("feat!: rewrite", body) == Bump.PATCH


def test_release_as_inside_an_override_wins() -> None:
    body = "Release-As: 1.2.3\n\nBEGIN_COMMIT_OVERRIDE\nfix: tidy\n\nRelease-As: 4.5.6\nEND_COMMIT_OVERRIDE"
    assert rlc.release_as("fix: tidy", body) == "4.5.6"


def test_touched_dirs_attributes_files_to_their_package() -> None:
    dirs = ["core/cli", "core/kit", "plugins/agents", "e2e"]
    files = [
        "core/kit/src/tai42_kit/app.py",
        "plugins/agents/tai-plugin.yml",
        "README.md",
        ".github/workflows/ci.yml",
    ]
    assert rlc.touched_dirs(files, dirs) == {"core/kit", "plugins/agents"}


def test_touched_dirs_picks_the_longest_matching_dir() -> None:
    # A nested package dir wins over a shorter ancestor that also matches.
    dirs = ["plugins", "plugins/agents"]
    assert rlc.touched_dirs(["plugins/agents/src/x.py"], dirs) == {"plugins/agents"}


def test_scripts_and_workflow_change_touches_no_package() -> None:
    dirs = ["core/cli", "plugins/agents", "e2e"]
    files = ["scripts/release_label_check.py", ".github/workflows/release-label.yml"]
    assert rlc.touched_dirs(files, dirs) == set()


def test_project_version_semver_for_a_one_point_x_package() -> None:
    kw = {"bump_minor_pre_major": False, "bump_patch_for_minor_pre_major": False}
    assert rlc.project_version("7.0.2", Bump.PATCH, **kw) == "7.0.3"
    assert rlc.project_version("7.0.2", Bump.MINOR, **kw) == "7.1.0"
    assert rlc.project_version("7.0.2", Bump.MAJOR, **kw) == "8.0.0"


def test_project_version_none_bump_is_no_release() -> None:
    assert (
        rlc.project_version("7.0.2", Bump.NONE, bump_minor_pre_major=False, bump_patch_for_minor_pre_major=False)
        is None
    )


def test_project_version_zero_x_default_flags_follow_plain_semver() -> None:
    # Both pre-major flags off (the repo's configured default): a breaking change on a
    # 0.x package graduates it to 1.0.0, a feat is a minor, a fix a patch.
    kw = {"bump_minor_pre_major": False, "bump_patch_for_minor_pre_major": False}
    assert rlc.project_version("0.5.2", Bump.MAJOR, **kw) == "1.0.0"
    assert rlc.project_version("0.5.2", Bump.MINOR, **kw) == "0.6.0"
    assert rlc.project_version("0.5.2", Bump.PATCH, **kw) == "0.5.3"


def test_project_version_zero_x_pre_major_flags_redirect_the_bump() -> None:
    # bump-minor-pre-major sends a breaking change to a minor; bump-patch-for-minor-pre-major
    # sends a feat to a patch — both only while below 1.0.0.
    assert (
        rlc.project_version("0.5.2", Bump.MAJOR, bump_minor_pre_major=True, bump_patch_for_minor_pre_major=False)
        == "0.6.0"
    )
    assert (
        rlc.project_version("0.5.2", Bump.MINOR, bump_minor_pre_major=False, bump_patch_for_minor_pre_major=True)
        == "0.5.3"
    )


def test_load_packages_applies_global_pre_major_defaults(tmp_path) -> None:
    config = tmp_path / "release-please-config.json"
    config.write_text('{"bump-minor-pre-major": true, "packages": {"core/kit": {"package-name": "tai42-kit"}}}')
    packages = rlc.load_packages(config)
    assert packages["core/kit"]["package-name"] == "tai42-kit"
    assert packages["core/kit"][rlc._BUMP_MINOR_PRE_MAJOR] is True
    assert packages["core/kit"][rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR] is False


def test_load_packages_lets_a_package_override_the_global_flag(tmp_path) -> None:
    config = tmp_path / "release-please-config.json"
    config.write_text(
        '{"bump-minor-pre-major": true, "packages": {"a": {"package-name": "a", "bump-minor-pre-major": false}}}'
    )
    packages = rlc.load_packages(config)
    assert packages["a"][rlc._BUMP_MINOR_PRE_MAJOR] is False


def test_check_gates_every_touched_releasing_package(monkeypatch, tmp_path) -> None:
    # Multi-package attribution: a breaking PR touching two packages runs the gate for
    # each at its projected major version; a touched package that projects no release
    # (and an untouched one) are not gated.
    packages = {
        "core/kit": {
            "package-name": "tai42-kit",
            rlc._BUMP_MINOR_PRE_MAJOR: False,
            rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR: False,
        },
        "plugins/agents": {
            "package-name": "tai42-agents",
            rlc._BUMP_MINOR_PRE_MAJOR: False,
            rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR: False,
        },
        "core/cli": {
            "package-name": "tai42-cli",
            rlc._BUMP_MINOR_PRE_MAJOR: False,
            rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR: False,
        },
    }
    manifest = {"core/kit": "7.0.2", "plugins/agents": "8.0.2", "core/cli": "11.0.2"}
    _packaged(tmp_path, "core/kit", "plugins/agents", "core/cli")
    calls: list[tuple[str, str, str]] = []

    def fake_gate(package: str, directory: str, version: str, repo_root) -> tuple[int, str]:
        calls.append((package, directory, version))
        return 0, f"{package}: gate passes.\n"

    monkeypatch.setattr(rlc, "_run_gate", fake_gate)
    code = rlc.check(
        "feat!: drop symbols",
        "",
        ["core/kit/src/a.py", "plugins/agents/src/b.py"],
        packages,
        manifest,
        tmp_path,
    )
    assert code == 0
    assert sorted(calls) == [("tai42-agents", "plugins/agents", "9.0.0"), ("tai42-kit", "core/kit", "8.0.0")]


def test_check_fails_when_a_gate_refuses(monkeypatch, tmp_path) -> None:
    packages = {
        "core/kit": {
            "package-name": "tai42-kit",
            rlc._BUMP_MINOR_PRE_MAJOR: False,
            rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR: False,
        }
    }
    manifest = {"core/kit": "7.0.2"}
    _packaged(tmp_path, "core/kit")
    monkeypatch.setattr(
        rlc,
        "_run_gate",
        lambda *a: (1, "::error::tai42-kit 7.0.3 is a patch bump but carries breaking surface changes\n"),
    )
    code = rlc.check("fix(kit): remove a public method", "", ["core/kit/src/a.py"], packages, manifest, tmp_path)
    assert code == 1


def test_check_passes_when_no_package_is_touched(monkeypatch, tmp_path) -> None:
    packages = {
        "core/kit": {
            "package-name": "tai42-kit",
            rlc._BUMP_MINOR_PRE_MAJOR: False,
            rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR: False,
        }
    }

    def fail_gate(*_a) -> tuple[int, str]:
        raise AssertionError("the gate must not run when no package is touched")

    monkeypatch.setattr(rlc, "_run_gate", fail_gate)
    code = rlc.check(
        "ci: add a workflow", "", ["scripts/x.py", ".github/workflows/y.yml"], packages, {"core/kit": "7.0.2"}, tmp_path
    )
    assert code == 0


def test_check_passes_when_a_touched_package_projects_no_release(monkeypatch, tmp_path) -> None:
    packages = {
        "core/kit": {
            "package-name": "tai42-kit",
            rlc._BUMP_MINOR_PRE_MAJOR: False,
            rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR: False,
        }
    }

    def fail_gate(*_a) -> tuple[int, str]:
        raise AssertionError("a chore touch projects no release, so the gate must not run")

    monkeypatch.setattr(rlc, "_run_gate", fail_gate)
    code = rlc.check(
        "chore(kit): reformat comments", "", ["core/kit/src/a.py"], packages, {"core/kit": "7.0.2"}, tmp_path
    )
    assert code == 0


def test_release_as_footer_forces_the_version() -> None:
    assert rlc.release_as("fix: a fix", "Release-As: 9.9.9") == "9.9.9"
    assert rlc.release_as("fix: a fix", "Release-As: v9.9.9") == "9.9.9"  # a leading v is stripped
    assert rlc.release_as("fix: a fix", "no footer here") is None


def test_check_release_as_forces_every_touched_package_version(monkeypatch, tmp_path) -> None:
    # A Release-As footer overrides the projected bump: the gate runs at the forced
    # version for each touched package, whatever the title's bump would have been.
    (tmp_path / "core" / "kit").mkdir(parents=True)
    (tmp_path / "core" / "kit" / "pyproject.toml").write_text("[project]\n")
    packages = {
        "core/kit": {
            "package-name": "tai42-kit",
            rlc._BUMP_MINOR_PRE_MAJOR: False,
            rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR: False,
        }
    }
    manifest = {"core/kit": "7.0.2"}
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(rlc, "_run_gate", lambda p, d, v, r: (calls.append((p, d, v)), (0, "gate passes.\n"))[1])
    code = rlc.check("fix(kit): a small fix", "Release-As: 12.0.0", ["core/kit/src/a.py"], packages, manifest, tmp_path)
    assert code == 0
    assert calls == [("tai42-kit", "core/kit", "12.0.0")]


def test_check_skips_a_descriptor_only_package(monkeypatch, tmp_path) -> None:
    # A touched dir without a pyproject.toml is descriptor-only (no Python surface): the
    # gate is not run for it, but a packaged sibling in the same PR still is.
    (tmp_path / "core" / "kit").mkdir(parents=True)
    (tmp_path / "core" / "kit" / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "plugins" / "connector-atlassian").mkdir(parents=True)  # no pyproject.toml
    packages = {
        "core/kit": {
            "package-name": "tai42-kit",
            rlc._BUMP_MINOR_PRE_MAJOR: False,
            rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR: False,
        },
        "plugins/connector-atlassian": {
            "package-name": "tai42-connector-atlassian",
            rlc._BUMP_MINOR_PRE_MAJOR: False,
            rlc._BUMP_PATCH_FOR_MINOR_PRE_MAJOR: False,
        },
    }
    manifest = {"core/kit": "7.0.2", "plugins/connector-atlassian": "2.0.12"}
    calls: list[str] = []
    monkeypatch.setattr(rlc, "_run_gate", lambda p, d, v, r: (calls.append(p), (0, "gate passes.\n"))[1])
    code = rlc.check(
        "fix: a fix",
        "",
        ["core/kit/src/a.py", "plugins/connector-atlassian/tai-plugin.yml"],
        packages,
        manifest,
        tmp_path,
    )
    assert code == 0
    assert calls == ["tai42-kit"]  # the descriptor-only connector was skipped, not gated
