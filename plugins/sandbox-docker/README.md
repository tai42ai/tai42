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
`unix:///var/run/...` socket path or a `tcp://host:port`) and is the only variable
that enters the recycle-pinned app env; the mTLS client certificates are read from
the canonical `/certs/client` mount. Resource-cap fallbacks
(`SANDBOX_DOCKER_DEFAULT_CPU` / `SANDBOX_DOCKER_DEFAULT_MEMORY_MB`), the image
`SANDBOX_DOCKER_PULL_POLICY`, and the shared TTL / reap / exec-timeout knobs round
out the group.

## Security model

- Remote engine over mTLS — no host Docker socket, no privilege in the app
  container.
- Per-session containers hardened with `no-new-privileges`, all capabilities
  dropped, never privileged, and no host bind mount.
- Single-workspace-mount isolation invariant: a session mounts ONLY its own
  workspace volume and can never read the engine's mTLS client identity.
- Network tiers `none` / `internal` / `egress` map onto the engine's network mode;
  egress default is OPEN, so tool-result data is exfiltratable under open egress
  (the egress firewall is provisioned at the tai-distribution layer).

## Durability model

An ephemeral session's workspace is an anonymous volume reaped with the session; a
persistent session binds a durable named `tai-sbx-<workspace_key>` volume that
survives the session and its reap, removed only by an explicit unforced teardown.
The durable store is provisioned at the tai-distribution layer.

See `docs/index.mdx` and the tai-docs operate page for the full settings table and
deployment topology.

The current release line tracks the **7.x contract** (`tai42-contract>=7,<8`).
