# AGENTS.md

Contributor and agent rules for this repository. Terse by design.

This is one uv workspace: `core/*` (contract, kit, skeleton), `plugins/*`, and
`e2e`. Workspace siblings resolve in-tree — never from the index.

## Commits

Conventional Commits, scoped by package directory. The type sets the release:

| Type | Release |
| --- | --- |
| `fix:` | patch |
| `feat:` | minor |
| `feat!:` or a `BREAKING CHANGE:` footer | major |
| `chore:` `docs:` `test:` `ci:` `refactor:` `perf:` `build:` `style:` | none |

release-please runs in manifest mode: it parses merged commits, raises one
release PR, and on merge tags each changed package `tai42-<name>-v<version>` and
publishes it. Non-conforming commits and PR titles fail the `commitlint` check.

## Build, test, lint

```sh
uv sync --locked
uv run --package <package> ruff check .
uv run --package <package> ruff format --check .
uv run --package <package> pyright
uv run --package <package> pytest --cov --cov-report=term-missing
```

Run each package's checks with its own directory as cwd so its coverage floor,
`filterwarnings`, markers, ruff, and pyright configs bind.

## Comments and docs

Terse, constraint-only, present tense. State the constraint and why it holds, not
what changed. No history notes, no plan/ticket/mission references, no
cross-repository references.

## Rules

- No `CHANGELOG.md` edits: notes are generated onto the GitHub Release.
- Loud errors: a failure fails the run. No silent fallbacks, no `|| true`, no
  swallowed exceptions, no compatibility shims.
- The workflows under `.github/workflows/` are the source of truth for commands;
  keep this file in step with them.
- Per-package ruff `banned-api` walls (e.g. plugins ban `tai42_skeleton`) are the
  only guard against illegal cross-package imports now that the workspace makes
  them physically importable — never delete them.
