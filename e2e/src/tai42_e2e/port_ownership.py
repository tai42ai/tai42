"""Whether each port-binding process of a stack owns the port the stack allocated it, and the heal when it does not.

The allocator probes a free ephemeral port then closes the socket before the child binds it,
so a foreign process on the shared host can seize that port in the gap. :func:`ensure_own_ports`
runs at the stack's one boot seam: it waits until each port-binding process holds its own port
and, on a collision, re-allocates a fresh port and respawns the process.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from tai42_e2e import child_env, ports, spawning
from tai42_e2e.waiting import wait_for

if TYPE_CHECKING:
    from tai42_e2e.procs import ProcessHandle
    from tai42_e2e.stack import TaiStack

logger = logging.getLogger(__name__)

# How many fresh ports one process may be handed before its bind is declared a
# genuine failure rather than an ephemeral-port collision.
PORT_OWNERSHIP_ATTEMPTS = 6


def ensure_own_ports(stack: TaiStack) -> None:
    """Confirm every port-binding process listens on the port ``stack`` allocated it, healing a collision.

    A foreign process can seize an allocated port before the child binds it; the child then
    fails to bind (``EADDRINUSE``) and exits. A busless stack's HTTP-only readiness would pass
    against the foreign listener and the test would talk to the wrong server. For each spawned
    process that binds a port — the serve fleet, the embed host, the metrics server alike —
    wait until the port is held by this process's own session; on the child's bind-failure
    exit, or a foreign pid holding the port, re-allocate a fresh port and respawn, and after
    the attempts are spent raise loudly naming the port and the foreign holder. Runs at the
    one boot seam, so every stack shape is covered without a per-stack copy.
    """
    for name in [n for n in list(stack.procs) if stack.ports_for(n)]:
        _heal_process_port(stack, name)


def pids_in_session(pids: list[int], leader_pid: int) -> bool:
    """Whether every listener pid belongs to ``leader_pid``'s session — the spawned
    master is a session leader (``start_new_session=True``), so its sid equals its pid
    and its uvicorn worker children inherit that session. A pid that vanished mid-check
    is skipped; confirming none is not ownership."""
    confirmed = False
    for pid in pids:
        try:
            if os.getsid(pid) != leader_pid:
                return False
        except (ProcessLookupError, PermissionError):
            continue
        confirmed = True
    return confirmed


def _heal_process_port(stack: TaiStack, name: str) -> None:
    for _ in range(PORT_OWNERSHIP_ATTEMPTS):
        port = stack.ports_for(name)[0]
        if _await_port_binding(stack, name, port) != "collision":
            # "owned": the process holds its own port. "released": the child exited for
            # its OWN reason (not a bind collision) — leave it untouched so readiness
            # surfaces the child's failure unchanged; NEVER respawn a child that died on
            # its own, and log no heal warning.
            return
        logger.warning(
            "stack %r: process %r did not own its port %d (foreign pid(s) %s) — re-allocating and respawning",
            stack.config.name,
            name,
            port,
            _foreign_holder_text(port),
        )
        _reallocate_and_respawn(stack, name, port)
    port = stack.ports_for(name)[0]
    raise RuntimeError(
        f"stack {stack.config.name!r}: process {name!r} could not bind an owned port after "
        f"{PORT_OWNERSHIP_ATTEMPTS} re-allocations — port {port} held by foreign pid(s) "
        f"{_foreign_holder_text(port)}"
    )


def _foreign_holder_text(port: int) -> str:
    """The pid(s) holding ``port`` as a plain, comma-joined string for a loud error, or
    ``unknown`` when no diagnostic tool could attribute the listener."""
    pids = ports.listening_pids(port)
    return ", ".join(str(pid) for pid in pids) if pids else "unknown"


def _await_port_binding(stack: TaiStack, name: str, port: int) -> str:
    """Decide the port's fate, distinguishing a bind COLLISION (the heal's to fix) from
    the child's own early exit (readiness's to surface):

    - ``"owned"`` — a listener in this process's session holds the port.
    - ``"collision"`` — a FOREIGN pid holds the port, or the child exited having logged
      the bind error (``EADDRINUSE``); re-allocate and respawn.
    - ``"released"`` — the child exited for its OWN reason with no foreign listener on
      the port (a bad manifest, a missing secret); leave it so readiness raises the
      child's failure unchanged.

    A process that never begins listening within the boot timeout is a genuine bind
    hang and raises through :func:`wait_for`."""
    handle = stack.procs[name]

    def decide() -> str | None:
        # Cheap gate first: only reach for the (subprocess-priced) pid attribution once
        # something is actually listening on the port.
        if ports.is_free(port):
            if handle.is_running():
                return None  # nothing bound yet; keep waiting
            # Exited without ever binding: only a recorded bind failure (a foreign
            # holder since gone) is the heal's; any other exit is the child's own.
            return "collision" if _exited_on_bind_error(handle) else "released"
        pids = ports.listening_pids(port)
        if pids:
            return "owned" if pids_in_session(pids, handle.pid) else "collision"
        # Bound but the diagnostic tools cannot attribute it: ours while our child is
        # alive (a foreign holder would have failed our bind and exited it); a foreign
        # holder once our child has exited.
        return "owned" if handle.is_running() else "collision"

    return wait_for(
        decide,
        deadline=stack.infra.settings.boot_timeout,
        message=f"process {name!r} never began listening on port {port}",
    )


def _exited_on_bind_error(handle: ProcessHandle) -> bool:
    """Whether an exited child's output shows it failed to BIND its port (``EADDRINUSE``)
    — the only early exit the port heal owns; every other exit is the child's own boot
    failure, which readiness surfaces unchanged."""
    tail = handle.log_tail().lower()
    return "address already in use" in tail or "[errno 98]" in tail


def _reallocate_and_respawn(stack: TaiStack, name: str, old_port: int) -> None:
    """Reap the child, drop the lost port, allocate a fresh one, rewrite the spec's
    ``--port``, refresh every port-derived env value, and respawn under the same name."""
    stack.procs[name].terminate()
    if old_port in stack.allocated_ports:
        stack.allocated_ports.remove(old_port)
    # The lost port is held by a foreign process, so do NOT assert it frees.
    ports.release_port(old_port)
    new_port = ports.allocate_port()
    stack.allocated_ports.append(new_port)
    stack.set_port(name, new_port)
    argv = stack.specs[name].argv
    for i in range(len(argv) - 1):
        if argv[i] == "--port":
            argv[i + 1] = str(new_port)
            break
    # The port-derived env (origin allowlist, own-origin, replica-B origin — see
    # ``child_env.dynamic_env``) was baked from the OLD port into every spec's process
    # env. Re-derive it now so neither this process nor any already-running sibling that
    # inherited a port-derived value advertises or allows the seized port; the
    # freshly-terminated target then respawns with it.
    _refresh_port_derived_env(stack, target=name)
    spawning.spawn(stack, stack.specs[name])


def _refresh_port_derived_env(stack: TaiStack, *, target: str) -> None:
    """Re-derive the port-keyed env into every spec, respawning each RUNNING process
    whose port-derived values changed (they are deployment env, so a process takes a
    new value only at its spawn).

    One seam over ``child_env.dynamic_env`` (no per-profile branch): bus keys do not
    depend on a port so they never trigger a respawn; only a profile that carries a
    port-derived origin (all-ports allowlist, own-origin, replica-B origin) has a
    process to refresh. The ``target`` was reaped by the caller and is respawned by it
    with the updated spec, so it is not respawned here even if still winding down.
    """
    fresh = child_env.dynamic_env(stack)
    for pname, spec in list(stack.specs.items()):
        if all(spec.env.get(key) == value for key, value in fresh.items()):
            continue
        spec.env.update(fresh)
        if pname != target and stack.procs[pname].is_running():
            stack.procs[pname].terminate()
            spawning.spawn(stack, spec)
