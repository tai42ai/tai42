"""The backend-needs-bus invariant fires on the runtime config paths, not only at boot.

Both halves run on a busless single-worker stack (the ``bare`` profile: MULTIWORKER(1),
no backend, so the harness wires it NO bus):

* the WRITE path — a config mutation that ADDS a ``backend_module`` is rejected 4xx
  naming ``TAI_BUS_REDIS_URL``, and NOTHING persists (the manifest file is byte-identical
  and no backend registers); and
* the RELOAD path (the reload-time twin) — an out-of-band manifest edit that adds a
  backend, then a fleet reload, fails LOUDLY (5xx) with the refusal naming the setting
  in the serving worker's LOG, and the registries are NOT rebuilt (the backend the
  edit named never comes up).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import yaml

from tai42_e2e import wait_for
from tai42_e2e.manifests import build_bare_stack
from tai42_e2e.stack import TaiStack

from ._fleet import manifest_file

pytestmark = pytest.mark.backendless

_BUS_VAR = "TAI_BUS_REDIS_URL"
# A backend module string for the mutation. It is never imported: the invariant fires
# on the manifest REGISTERING a backend, before any module load, so the string only has
# to be truthy (mirrors the boot-refusal probe).
_UNREACHED_BACKEND = "b8_probe.backend_never_imported"


async def test_write_adding_backend_without_bus_is_rejected(fresh_stack: Callable[..., TaiStack]) -> None:
    """Write path: ``POST /api/manifest/replace`` adding a ``backend_module`` on a
    busless stack is a loud 4xx naming ``TAI_BUS_REDIS_URL`` and persists NOTHING."""
    stack = fresh_stack(build_bare_stack)
    api = stack.api()
    path = manifest_file(stack)
    before = path.read_bytes()

    document = yaml.safe_load(before.decode()) or {}
    document["backend_module"] = _UNREACHED_BACKEND
    resp = await api.request_raw("POST", "/api/manifest/replace", json={"manifest_text": yaml.safe_dump(document)})

    assert resp.status_code == 400, f"a busless backend mutation must be a loud 400: {resp.status_code} {resp.text}"
    assert _BUS_VAR in resp.text, resp.text
    assert "task backend" in resp.text, resp.text
    # Nothing persisted: the manifest on disk is byte-identical and no backend registers.
    assert path.read_bytes() == before, "a rejected backend mutation must not have touched the manifest"
    assert (await api.get("/api/backend"))["present"] is False


async def test_out_of_band_backend_edit_fails_the_reload(fresh_stack: Callable[..., TaiStack]) -> None:
    """Reload twin: an out-of-band manifest edit adding a backend, then ``POST
    /api/fleet/reload-config``, fails LOUDLY; the reload-time re-check runs BEFORE the
    registries rebuild — and BEFORE the named module is imported — so the named backend
    never comes up.

    The reload path surfaces the refusal as a loud 5xx (the reload-time
    ``require_bus_for_backend`` raise becomes a ``FleetBroadcastError``). The 5xx body is
    Starlette's generic ``Internal Server Error`` — the setting is NOT in it — so the
    refusal is read where it actually lands: the serving worker's LOG. That log line is
    the discriminating signal. The re-check fires ahead of the module import, so the
    non-importable edit never reaches an ``ImportError``; were the re-check gone, the
    reload would instead die importing the bogus module — an error that names neither
    the setting nor the backend rule — and this assertion would go red."""
    stack = fresh_stack(build_bare_stack)
    api = stack.api()
    path = manifest_file(stack)

    document = yaml.safe_load(path.read_text()) or {}
    document["backend_module"] = _UNREACHED_BACKEND
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    resp = await api.request_raw("POST", "/api/fleet/reload-config", json={"targets": None})
    assert resp.status_code >= 500, f"the reload of a busless backend edit must fail loudly: {resp.status_code}"
    # Registries were NOT rebuilt: the backend the edit named never registered.
    assert (await api.get("/api/backend"))["present"] is False
    # The reload-time bus re-check is the discriminating cause: its refusal must land in
    # the serving worker's log naming the bus setting AND the backend rule. The refusal
    # is logged as the failed request unwinds — a beat after the 5xx flushes — so poll
    # the log (never sleep) for it. An ImportError from a regressed re-check would name
    # neither string, so this never goes green on the regression it guards.
    serve_log = stack.process("serve").log_path

    def refusal_in_log() -> str:
        text = serve_log.read_text(encoding="utf-8", errors="replace")
        return text if _BUS_VAR in text else ""

    logged = wait_for(refusal_in_log, deadline=10.0, message="the reload refusal never reached the serve log")
    assert "task backend" in logged, logged
