# Contributing to tai42

tai42 is one repository and one uv workspace: `core/*` (contract, kit, cli,
skeleton), `plugins/*`, and `e2e`. A change lives in the package directory it
touches, and the `tai42-*` dependencies between packages resolve in-tree through
the workspace.

## Setup

```bash
git clone https://github.com/tai42ai/tai42
cd tai42
uv sync --locked
```

One clone, one `uv sync`. Workspace members install editable into a single
`.venv`; sibling `tai42-*` requirements resolve to the in-tree packages, never
from the index. To iterate on a single package:

```bash
uv sync --locked --package tai42-<name>
uv run --package tai42-<name> ruff check .
uv run --package tai42-<name> ruff format --check .
uv run --package tai42-<name> pyright
uv run --package tai42-<name> pytest --cov --cov-report=term-missing
```

Run each package's checks from its own directory so its coverage floor,
`filterwarnings`, markers, and ruff/pyright config bind. Running `pyright` from
the repository root is unsupported: there is no root config, so pyright falls
back to its defaults (standard mode, `reportMissingImports=error`) and reports
hundreds of false positives. Always run it per package so that member's
`[tool.pyright]` binds; `scripts/pyright-all.sh` sweeps every member the way CI
does.

## Dependency resolution

The single root `uv.lock` is the workspace resolution: `[tool.uv.sources]` in
each member points sibling `tai42-*` requirements at `{ workspace = true }`, and
the lock records that in-tree resolution. `uv lock` regenerates it; CI asserts it
with `uv sync --locked`. The workspace sources are the resolution — the lock and
the sources agree by construction.

An in-repo version bump is atomic: raising a package's version updates every
in-tree consumer in the same commit.

## Naming

PyPI is a flat namespace, so distributions carry the `tai42-` prefix; import
packages follow as `tai42_<name>`.

| Surface | Form |
| --- | --- |
| Distribution — PyPI, `pip install`, dependency pins | `tai42-<name>` |
| Import package | `tai42_<name>` |
| Repository path | `core/<name>`, `plugins/<name>`, `e2e` |

Deliberately neither, and never renamed: the `tai` CLI command (`tai42` is an
alias), the Prometheus metric namespace (`tai_tool_*`), `TAI_*` environment
variables, and the `tai-plugin.yml` descriptor filename.

## Commits and releases

Conventional Commits, scoped by package directory. release-please runs in
manifest mode: one combined release PR carries every changed package, and
merging it tags each one `tai42-<name>-v<version>`. Pull requests are
squash-merged, and release-please reads the squash commit GitHub writes: a
single-commit PR squashes to that commit's message, a multi-commit PR to the PR
title followed by the commits' messages. The PR body is never part of it; a
`BEGIN_COMMIT_OVERRIDE` … `END_COMMIT_OVERRIDE` block in the PR body replaces
the message. The bump follows release-please's rules for each package's release
type. Every type: a breaking change (`!` on the type or a `BREAKING CHANGE:`
footer) → major, `feat:` → minor, `fix:`, `perf:` and `revert:` → patch.
`docs:` and `deps:` → patch for the `python` packages and nothing for the
descriptor-only `simple` connector plugins. Other types release nothing. A
package's `tests/` and `examples/` attribute nothing: a change under them alone
releases nothing for that package. Non-conforming commits and PR titles fail the
`commitlint` check, as does a `<` that opens markup (followed by a letter, `/`,
`!` or `?`) in a commit header, a `BREAKING CHANGE:` footer or the PR title: the
release notes are parsed as HTML, so write placeholders without angle brackets.

First-party dependency ranges and `tai-plugin.yml` `contract:` pins are derived
from the sibling versions by `scripts/range_sync.py` (`>=major.minor,<major+1`).
The release PR keeps them current: release-please's `python-workspace` plugin
(`scripts/release-please/python-workspace.js`) asks `range_sync.py --query`
which members the release's versions move ranges for and adds a patch release
of each one to the same PR, with the moved ranges in its notes; the PR's
follow-up commit runs `range_sync.py` and `uv lock`. CI runs
`range_sync.py --check` on every change, and the release workflow runs it before
building, so no package publishes a range that differs from the one its
siblings derive. Before that follow-up commit writes the ranges,
`scripts/release_train_check.py` fails the run unless every unit whose ranges
or contract pins are about to move is one the release PR releases.

Pull requests to `main` merge only when they are up to date with it (the branch
protection's "Require branches to be up to date before merging"). The release
PR is rebuilt from the tip of `main` on every push and its ranges are derived
from that tip, so the requirement makes the release PR merge only the tree its
CI proved, and makes a development PR's `range_sync.py --check` run against the
`main` it lands on. A fork sets the same protection on its own `main`.

The `release-label` check asks release-please what it would mint for the pull
request on top of everything unreleased on main and runs the release API-diff
gate at that version; release-please's own release PR (its
`release-please--branches--main` branch in this repository) is gated at the
versions in its manifest. A breaking public-API change under a version that
cannot carry it is refused before the merge, not after the tag. The check reads,
with the workflow's `GITHUB_TOKEN`, the pull request GitHub associates with each
unreleased commit on main, as release-please does. It models one combined
release PR and a squash merge, so it refuses to run while the release-please
config opens separate release PRs or the repository allows merge commits or
rebase merges.

release-please is pinned once, in `scripts/release-please/package-lock.json`,
for both the `release-label` check and the release workflow. Install it with
`npm ci --prefix scripts/release-please` before running `pytest scripts/tests`.

## Cross-repo e2e

The browser lanes test this repo against tai-studio (and tai-marketplace). To
pair a change here with one in tai-studio, add a `tai-studio-ref: <branch-or-sha>`
line to the pull request body and the `ui-e2e` lane checks that studio ref out
instead of `main`. In the other direction, a tai-studio or tai-marketplace pull
request adds a `tai42-ref: <branch-or-sha>` line and the `fleet-e2e` receiver
runs this repo's harness at that ref. Either line is a plain body field, not a
Conventional-Commits header, so it never affects the release the pull request
projects; omit it and the paired repo stays at `main`. The value must be a branch
name or a 40-character commit sha — never a fully-qualified `refs/…` ref, which
is refused.

## License

By contributing you agree your contributions are licensed under Apache-2.0.
