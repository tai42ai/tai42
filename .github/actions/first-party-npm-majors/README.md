# first-party npm majors

A repo-integrity check that fails when a first-party scoped npm dependency is a
**full major version behind** its latest published release, and stays quiet for
anything smaller. The automated update bot already raises minor and patch bumps;
this closes the one hole it leaves open — a major bump the bot does not raise
that then sits ignored.

Accepted cost: when a scoped package publishes a new major, every consumer still
pinned to the previous major reds this check until its pin is upgraded. That is
the point — the lag is made visible at the gate instead of drifting silently.

## What it gates

Every `package.json` under the checkout (recursively, excluding `node_modules`,
`dist` and `.git`) is scanned across `dependencies`, `devDependencies` and
`peerDependencies` for a dependency whose name matches the configured scope.

For each such dependency pinned to a **registry range** the highest major line
the range can install is compared to the latest published major line. A range
that cannot reach the latest major fails; a range already on (or ahead of) the
latest major passes quietly.

- Fails: `^18.0.0` when the latest release is `19.x`.
- Passes: `^19.3.0` when the latest release is `19.5.0` (same major) or
  `19.4.2` (minor/patch behind — the bot's job) or `^20.0.0` (ahead).

A dependency pinned to an internal source — `workspace:`, `link:`, `file:` or
`portal:` — is not a registry pin and is skipped. When no matching dependency
(or no `package.json`) is present the check self-skips and passes.

### 0.x versions

A `0.x` release follows npm caret semantics, where the `0.MINOR` line is the
major line: a `0.1` → `0.2` jump is a major step. So `^0.1.0` fails when the
latest release is `0.2.0`, and passes against `0.1.7`. Every `0.x` sorts below
every `>= 1` release.

## Inputs (settings)

| Input | Default | Meaning |
| --- | --- | --- |
| `scope` | `@tai42` | The npm scope whose dependencies are gated. |
| `registry` | `https://registry.npmjs.org` | The registry base URL queried for the latest published version. |

Both are plain settings with neutral defaults, so a fork points the check at its
own scope and registry without editing the script.

## Using it

```yaml
- uses: actions/checkout@v7
- uses: ./.github/actions/first-party-npm-majors
  with:
    scope: "@your-scope"          # optional, defaults to @tai42
    registry: "https://registry.npmjs.org"  # optional
```

The composite action sets up Node 22 and runs the check against the checkout.

## Errors

A registry or network failure, an unreadable range, or an unparseable
`package.json` fails the check with a clear message and a non-zero exit — never
a silent pass. The script evaluates exact, caret (`^`), tilde (`~`) and
`x`-range pins (the forms the update bot and hand edits produce); a multi-
comparator or union range (`>=1 <2`, `1 || 2`) raises loudly rather than guess a
verdict.

Exit codes: `0` current or skipped, `1` at least one pin a full major behind,
`2` an operational error.

## Tests

`check.test.mjs` runs with the Node test runner against synthetic fixtures and a
mock registry (no network):

```sh
node --test '.github/actions/first-party-npm-majors/*.test.mjs'
```
