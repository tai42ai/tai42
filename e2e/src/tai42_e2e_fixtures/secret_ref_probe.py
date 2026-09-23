"""SUT-side probes for the preset secret-reference doors.

A preset bakes a ``!ENV ${VAR}`` reference into a ``fixed_kwargs`` leaf; the server
resolves it at bind and forwards it to a probe tool. Two parameter typings prove both
sides of the mechanism:

* :func:`e2e_secret_ref_sink` takes a PERMISSIVE ``payload: Any`` parameter, so the
  resolved value reaches it as a wrapped ``SecretValue``: the live sync/MCP run-tool
  door reveals it to the caller (the reference resolved at dispatch) while a
  background-recorded run stores only the masked placeholder.
* :func:`e2e_secret_ref_typed_sink` takes a TYPED ``token: str`` parameter — the common
  real case (an API key / token). The resolved reference is revealed to its plain string
  so it passes the base tool's argument validation and reaches the typed parameter; the
  probe returns the received length and prefix (never the secret itself) so the suite can
  assert the resolved value arrived without recording it.

Generic vocab only. Registers at import, re-run on every boot/reload by the manifest
tool loader."""

from __future__ import annotations

from typing import Any

from tai42_contract.app import tai42_app


@tai42_app.tools.tool(tags={"e2e"})
def e2e_secret_ref_sink(payload: Any) -> dict:
    """Return ``payload`` unchanged — the probe for a preset secret reference."""
    return {"payload": payload}


@tai42_app.tools.tool(tags={"e2e"})
def e2e_secret_ref_typed_sink(token: str) -> dict:
    """Report that a resolved reference reached a ``str`` parameter, without echoing it.

    Returns the received value's runtime type, length and last character so the suite
    proves the resolved string arrived at a typed scalar parameter while the recorded
    result never carries the secret verbatim."""
    return {"type": type(token).__name__, "length": len(token), "last": token[-1:]}
