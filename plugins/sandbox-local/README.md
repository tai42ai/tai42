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
It is for a **trusted, single-tenant box**, not untrusted workloads. It accepts only
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
(`SANDBOX_LOCAL_REAP_INTERVAL_SECONDS` sets the platform reaper's sweep interval,
`SANDBOX_LOCAL_EXEC_DEFAULT_TIMEOUT_SECONDS` the timeout of a short exec that names
none). See
[`docs/index.mdx`](src/tai42_sandbox_local/docs/index.mdx) for the full table and
the durability model.

## Stopping a command

The provider starts every command in a process group of its own and keeps a small
shell process in that group, so the group keeps its id after the command itself has
exited. Stopping a command (a `kill()`, a timeout, a one-shot call that fails or is
cancelled, an interactive start that is cancelled, or the session's teardown, which
stops the session's interactive commands) kills its whole process group: the command
stops, and so does every process it started that is still in its group, even when
the command itself has already exited. After a `kill()`, an interactive command's
output still gives every chunk the provider had received from the command before the
kill, read or not, in order, before it ends. The teardown does not stop a one-shot
call: one still running when its session is torn down runs on until it ends or
reaches its timeout. For an interactive command the provider keeps that hold — one
shell process and one descriptor — until `kill()` or the session's teardown, so a
finished interactive command's group stays reachable until then; a consumer done
with a handle calls `kill()`. A stop never signals anything outside the command's
process group: the group is signalled only while the provider's shell process still
holds it, so a group id the host has since given to other processes is never
signalled. A process that left the command's group (a `setsid` daemon, for example)
is not stopped. Once a one-shot command has completed, the provider lets go of its
group, and processes the command left running keep running. Should the provider's
shell process have been ended from outside, a stop kills the command alone and logs
a warning that processes it started may still be running. The small shell process
the provider keeps in each command's process group ends when the provider lets the
group go and is reaped by the init process (PID 1); a container that runs the server
as its PID 1 therefore needs an init (`docker run --init`, or an init as the
entrypoint — the published `tai` image runs under `tini`).

## A command's input

The provider writes a command's stdin while the command runs. A command that closes
its stdin, or exits, before reading all of it has chosen not to read the rest: the
call returns as usual. A command that exits while a process it started still holds its
stdin, with input the provider has not written yet, fails instead: a one-shot call
raises a `SandboxError`, and an interactive command's output ends with a
`SandboxError` in place of its exit. A stop never causes such a failure and never
clears one: an interactive command whose exit cut its input before `kill()` still
ends its output with that `SandboxError`.
