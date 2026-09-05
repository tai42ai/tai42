# tai42-sandbox-local

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

A direct/host sandbox provider for the TAI ecosystem. It implements the
`tai42_contract.sandbox.Sandbox` surface over plain host subprocesses and the host
filesystem: it runs a session's code **directly on the host**, with no container
and no isolation. Exactly one sandbox provider is active per deployment, and the
operator picks direct/host execution by installing this provider instead of a
container provider — the consumers are provider-agnostic and acquire whichever is
installed.

## The TAI ecosystem

TAI is an open-source runtime for MCP tools, agents, and workflows. A `Sandbox` is
"where a session's code runs" — a pluggable provider the runtime acquires through
one seam. This package is one such provider (direct/host execution); any package
can back the same contract, so this repo is this provider's own full doc home, and
the documentation site covers the platform-level story:

- Sandbox concept: https://tai42.ai/concepts/sandboxes
- Deployment / operate: https://tai42.ai/operate

Its only tai-* dependencies are `tai42-contract` (the `Sandbox` ABC, the session /
stream models, `SandboxPolicy`, the error family, `PluginItemKind.SANDBOX`, and the
`tai42_app.sandboxes` facet) and `tai42-kit` (`ManagedSandbox` /
`ManagedSandboxSession` — the shared ledger, TTL/reap, orphan recovery, and the
session-create policy chokepoint — plus `SandboxDispatchSettings` and the
conformance suite). It pulls in no third-party runtime dependency: it drives the
host with the standard library only.

## Security model

This provider gives **no isolation**: `isolation="none"` means arbitrary session
code runs on the host with the host's filesystem, network, and secrets in reach.
It is for a **trusted, single-tenant box**, not untrusted flows. It accepts only
what it can honestly enforce and rejects the rest loudly:

- `isolation="none"` accepted; `container` / `vm` rejected. The operator isolation
  floor defaults to `container`, so a deployment installing this provider **must**
  set `TAI_MCP_SANDBOX_ISOLATION=none` or every session create rejects.
- `network="egress"` accepted; `none` / `internal` rejected.
- a `cpu` / `memory_mb` cap rejected.
- `image` is inert — the host is the execution environment; the operator installs
  the runtime on the host.

For enforced isolation or network lockdown, install a container provider instead.

## Configuration

The `SANDBOX_LOCAL_` env group: `SANDBOX_LOCAL_ROOT` (the host workspace root) and
`SANDBOX_LOCAL_BASE_PATH` (the clean-env `PATH`), plus the shared dispatch knobs
(`SANDBOX_LOCAL_DEFAULT_TTL_SECONDS`, `SANDBOX_LOCAL_REAP_INTERVAL_SECONDS`,
`SANDBOX_LOCAL_EXEC_DEFAULT_TIMEOUT_SECONDS`). See
[`docs/index.mdx`](src/tai42_sandbox_local/docs/index.mdx) for the full table and
the durability model.

The current release line tracks the **7.x contract** (`tai42-contract>=7,<8`).
