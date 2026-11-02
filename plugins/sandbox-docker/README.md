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

## Session image

A session image must provide `sh`, the `setsid`, `mkfifo`, `mv`, `rm`, `cat`, `kill`,
`readlink`, `ls` and `sleep` commands (`sleep` taking a fractional time), `/proc`, and a
writable `/tmp`. The provider runs every command inside a small shell wrapper that records
what the command's output is connected to, in `/tmp/tai-exec.<id>.out`, then starts the
command in a process group of its own and writes its process id to a file in `/tmp`; a
wrapper that cannot record the output's connection, or cannot create the small pipe file it
holds the command's group with, runs nothing and ends the exec with exit code 126 and a
line on its stderr. The wrapper keeps a small shell process in the command's group until
the wrapper ends, so the group keeps its id after the command itself has exited, and a stop
never signals a group id the container has since given to other processes. Stopping a
running command (on a kill, a timeout, a one-shot call that fails or is cancelled, or an
interactive start that fails or is cancelled while it attaches) reads the process-id file
and kills the command's process group: the command stops, and so does every process it
started that is still in its group, even when the command itself has already exited. A
process the command started that left its group is never stopped. The stop then looks
through `/proc` for any process that still holds the command's output, as such a process
may, and fails when it cannot read the open files of a process that could. The stop reports
what it found as a line of its own output, so the report reaches the provider as soon as it
is known, before the engine has recorded the stop's own exit. The wrapper survives the
kill, reaps the command and records its status at once, and a stop that killed the
command's group reports that status: a `kill()` ends the output with 137 for the command it
killed, or with the command's own exit code when it had exited before the kill landed, even
while a process the command started still holds its output. A stop whose command's status
does not come within 2 seconds of its kill, as when the wrapper was itself stopped or
killed, fails, since that status cannot be known. A command is running while its own
process runs or a process it started still holds its output. Once the command's own process
has exited, the wrapper waits while any process still holds the output, or while it cannot
read the open files of a process that could, then removes the process-id file and
`/tmp/tai-exec.<id>.out` and ends with the command's exit code; a stop while it waits kills
every process still in the command's group, as the command's timeout does, and reports
whether the output is still held. The start of any later exec in the session removes each
such file left in `/tmp` that nothing holds any more, as one left by a wrapper that was
itself killed; a later exec that cannot read or remove one fails with exit code 126 and a
line on its stderr, before its command starts. A stop that finds nothing to stop, as after
the wrapper has ended, or whose command the engine never started, leaves a small marker
file in `/tmp`, and so, rarely, does one that lands just as the wrapper ends. The session
container runs the engine's init as its main process, which reaps the small shell process
the wrapper keeps in each command's group and every process a command started that outlives
it. That kill has a fixed time limit of 5 seconds, and a further cancellation of the call
does not cut it short. A kill that does not finish in time fails with its time limit, noted
with what the stop had reported before then: its own error line, or the part of it that
arrived, or the engine's refusal of the stop. After a `kill()`, the output still gives
every chunk the provider had received before the kill, read or not, in order, before it
ends.

A command that ended within its timeout, nothing holding its output, is read to the end of
its output however late or slowly that is read, within the Docker engine's own limit: the
engine force-closes the output stream of a command about 2 s after the command exits, so a
command that ended within its timeout with more than roughly 1 MiB of output still pending
in the engine loses that output when its reader pauses for more than 2 s, and the plugin
cannot see that loss. A command whose own process ended within its timeout while a process
it started still holds its output is read to the end of that process's output too, within
the same limit from the moment nothing holds it. While such a reader stays paused, the
engine also holds the end of every other command's output in the same session, and their
exit codes, until that reader reads on or its output is closed: another command that ends
meanwhile ends its output with `SandboxError` once its own grace or its kill's time limit
has passed, and a `kill()` of such a command returns, since the stop's report does not
wait on the engine. A command still running at its timeout raises
`SandboxExecTimeoutError` there, whether its output is read or not and however slowly: its
output is cut there. When its output ends with the timeout's stop, the timeout counts
every byte of output the command gave, the output still on its way at the timeout
included; an output that has not ended within 4 s of the stop is noted on the timeout as a
count that may miss output. When a process the command started still holds the output
after the stop, so that it may still be running and writing, the timeout counts the output
received up to the stop, and the stop is logged and noted on the timeout. A timeout still
raises `SandboxExecTimeoutError`, and the other calls still raise their own error, when
that kill fails or does not finish in time: the kill is logged and noted on the error
raised, and so is a kill after which a process the command started still holds its output.
A `kill()` that fails or does not finish in time raises `SandboxError`; one whose
container is gone, as after the session's `destroy()`, or has stopped returns, since the
command ended with it, while one in a paused container raises. A `kill()` after which a
process the command started still holds its output ends the output at once: with the status
the stop reported when it killed the command's group, and with a `SandboxError` when it
found nothing left to kill, as after the wrapper was itself killed, since that command's
status cannot be known: the stop read none, and the engine records no exit code for a
command while a process it started holds its output. After a `kill()` that
finds nothing running and nothing holding the output, the output keeps the command's own
exit code when its stream ends within a short, fixed grace, and reports a `SandboxError`
when it does not; until then the command's stdin still takes writes and the end of input.
An output whose stream ends before the command's exit status is known raises
`SandboxError`, and the command's stdin then takes no more writes; the command may still
be running, and a `kill()` still stops it. Reading the output of a command whose container
is gone raises `SandboxError`. An image without a shell cannot run any command.

## Configuration

The `SANDBOX_DOCKER_` env group. `SANDBOX_DOCKER_HOST` is required (a
`unix:///var/run/...` socket path, a bare socket path, or a `tcp://host:port` (also
written `http://` or `https://`), a network host naming no port dialled on the
engine's 2376 when dialled over TLS (under mTLS, or an `https://` host) and 2375 when
dialled plain; any other form is refused). The mTLS client certificates are read
from files, by default on the canonical `/certs/client` mount
(`SANDBOX_DOCKER_TLS_CERT_PATH`, `SANDBOX_DOCKER_TLS_KEY_PATH` and
`SANDBOX_DOCKER_TLS_CA_PATH`, defaulting to `/certs/client/cert.pem`, `key.pem` and
`ca.pem`); the environment carries only those file paths, never the certificate or key
material. Resource-cap fallbacks (`SANDBOX_DOCKER_DEFAULT_CPU` /
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
