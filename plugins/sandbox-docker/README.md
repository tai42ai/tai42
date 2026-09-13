# tai42-sandbox-docker

Docker sandbox provider for the TAI ecosystem — per-session containers on a
REMOTE Docker engine over the Docker Engine API.

Each session runs as its own hardened container on a remote engine reached over
mTLS. The app container spawns no local process, holds no host Docker socket, and
mounts no host path; real work is driven through the engine's exec API against an
idle session container.

## Enable it

```yaml manifest.yml
sandbox_module: tai42_sandbox_docker
```

## Configuration

The `SANDBOX_DOCKER_` env group. `SANDBOX_DOCKER_HOST` is required (a
`unix:///var/run/...` socket path or a `tcp://host:port`); the mTLS client
certificates are read from the canonical `/certs/client` mount, never from the
environment. Resource-cap fallbacks (`SANDBOX_DOCKER_DEFAULT_CPU` /
`SANDBOX_DOCKER_DEFAULT_MEMORY_MB`), the image `SANDBOX_DOCKER_PULL_POLICY`, the
opt-in egress-firewall readiness probe (`SANDBOX_DOCKER_READINESS_PROBE_ENABLED` /
`SANDBOX_DOCKER_READINESS_PROBE_IMAGE` /
`SANDBOX_DOCKER_READINESS_PROBE_TIMEOUT_SECONDS`), and the shared TTL / reap /
exec-timeout knobs round out the group.

## Security model

- Remote engine over mTLS — no host Docker socket, no privilege in the app
  container.
- Per-session containers hardened with `no-new-privileges`, all capabilities
  dropped, never privileged, and no host bind mount.
- Single-workspace-mount isolation invariant: a session mounts ONLY its own
  workspace volume and can never read the engine's mTLS client identity.
- Network tiers `none` / `internal` / `egress` map onto the engine's network mode;
  egress default is OPEN, so tool-result data is exfiltratable under open egress
  (the egress firewall is provisioned by the deployment that runs the engine).
- Egress-firewall readiness (opt-in): with `SANDBOX_DOCKER_READINESS_PROBE_ENABLED`,
  a create first proves the engine's egress firewall is in force — a throwaway,
  unprivileged probe container on the egress tier must not reach the engine's
  control address and must reach its own resolver — and refuses the create loudly
  otherwise. Both probe targets are derived, so there is nothing to mis-set.

## Durability model

An ephemeral session's workspace is an anonymous volume reaped with the session; a
persistent session binds a durable named `tai-sbx-<workspace_key>` volume that
survives the session and its reap, removed only by an explicit unforced teardown.
The durable store is provisioned by the deployment that runs the engine.

See `docs/index.mdx` and the tai-docs operate page for the full settings table and
deployment topology.
