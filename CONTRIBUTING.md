# Contributing to tai42

tai42 is one repository and one uv workspace: `core/*` (contract, kit,
skeleton), `plugins/*`, and `e2e`. There is no multi-repo ecosystem to keep in
sync — a change lives in the package directory it touches, and the `tai42-*`
dependencies between packages resolve in-tree through the workspace.

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
`filterwarnings`, markers, and ruff/pyright config bind.

## Dependency resolution

The single root `uv.lock` is the workspace resolution: `[tool.uv.sources]` in
each member points sibling `tai42-*` requirements at `{ workspace = true }`, and
the lock records that in-tree resolution. `uv lock` regenerates it; CI asserts it
with `uv sync --locked`. There is no `UV_NO_SOURCES`, no per-repo lock, and no
disagreement between the lock and the sources to reconcile — the workspace
sources are the resolution.

An in-repo version bump is atomic: raising a package's version updates every
in-tree consumer in the same commit, so there is no cross-repo bump propagation.

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
manifest mode: one merged commit train, one release PR per changed package, tags
`tai42-<name>-v<version>`. `fix:` → patch, `feat:` → minor, `feat!:` or a
`BREAKING CHANGE:` footer → major; other types do not release. Non-conforming
commits and PR titles fail the `commitlint` check.

## License

By contributing you agree your contributions are licensed under Apache-2.0.
