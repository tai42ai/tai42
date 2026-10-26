"""A settings key the deployment sets belongs to the deployment.

A key with a non-empty value in the process env at startup is set by the deployment (a
container env, a unit file, the harness's own process env). Its value wins at boot and at
every reload, so the app:

* reports the field's ``value_source`` as ``"deployment"`` in the settings schema, with the
  deployment's value;
* refuses a write that sets it to a new value (a 400 naming the key) and stores nothing;
* ignores a stored copy of it at reload — the copy stays in the store, listed under
  ``deployment_keys`` by ``GET /api/config/env``, and the running value is the deployment's;
* lets a write carry that stored copy unchanged, or remove it.

The deployment key here is ``TAI_LOG_LEVEL`` (the kit ``LoggingSettings`` field, hot-class,
present on every leg), set in the stack's process env.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import child_env
from tai42_e2e.manifests import build_minimal_stack
from tai42_e2e.stack import TaiStack

_KEY = "TAI_LOG_LEVEL"
_DEPLOYMENT_VALUE = "WARNING"
_STORED_VALUE = "ERROR"

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.needs("process", "mutable", f"setting:{_KEY}={_DEPLOYMENT_VALUE}"),
]


def _deployment_stack(fresh_stack: Callable[..., TaiStack]) -> TaiStack:
    """A one-worker stack whose deployment env sets ``_KEY``."""
    stack = fresh_stack(build_minimal_stack, env_overrides={_KEY: _DEPLOYMENT_VALUE})
    assert _KEY not in stack.config.stored_env, "the deployment key must reach the app through the process env only"
    return stack


async def _schema_field(stack: TaiStack, env_var: str) -> dict[str, Any]:
    """The settings-schema row of the field read from ``env_var``."""
    schema = await stack.api().get("/api/config/settings-schema", retry_on_reloading=True)
    rows = [field for group in schema["groups"] for field in group["fields"] if field["env_var"] == env_var]
    assert len(rows) == 1, f"expected one settings-schema field for {env_var}: {rows}"
    return rows[0]


async def test_a_deployment_key_reports_its_deployment_value_and_refuses_a_write(
    fresh_stack: Callable[..., TaiStack],
) -> None:
    stack = _deployment_stack(fresh_stack)
    api = stack.api()

    field = await _schema_field(stack, _KEY)
    assert field["value_source"] == "deployment", field
    assert field["value"] == _DEPLOYMENT_VALUE, field

    before = await api.get("/api/config/env", retry_on_reloading=True)
    resp = await api.request_raw("POST", "/api/config/env", json={"env": {_KEY: _STORED_VALUE, "E2E_SIBLING": "x"}})
    assert resp.status_code == 400, resp.text
    error = resp.json()["error"]
    assert "Refusing an env write setting keys set by the deployment" in error, error
    assert _KEY in error, error
    # The refusal is whole-write: neither the deployment key nor its sibling was stored.
    assert await api.get("/api/config/env", retry_on_reloading=True) == before

    field = await _schema_field(stack, _KEY)
    assert (field["value_source"], field["value"]) == ("deployment", _DEPLOYMENT_VALUE), field


@pytest.mark.needs("files")
async def test_a_stored_copy_of_a_deployment_key_never_wins_and_may_be_carried_or_removed(
    fresh_stack: Callable[..., TaiStack],
) -> None:
    stack = _deployment_stack(fresh_stack)
    api = stack.api()

    # A copy of the deployment key lands in the store outside the app (a hand edit of the
    # ``.env``, or a store written before the deployment set the key), then a reload.
    stack.config.stored_env[_KEY] = _STORED_VALUE
    child_env.render_env_file(stack)
    await api.post("/api/config/reload", json={}, retry_on_reloading=True)

    view = await api.get("/api/config/env", retry_on_reloading=True)
    assert view["env"].get(_KEY) == _STORED_VALUE, view
    assert view["deployment_keys"] == [_KEY], view
    field = await _schema_field(stack, _KEY)
    assert (field["value_source"], field["value"]) == ("deployment", _DEPLOYMENT_VALUE), field

    # A write that carries the stored copy unchanged is accepted with the rest of its keys.
    await api.post(
        "/api/config/env", json={"env": {_KEY: _STORED_VALUE, "E2E_SIBLING": "kept"}}, retry_on_reloading=True
    )
    view = await api.get("/api/config/env", retry_on_reloading=True)
    assert view["env"].get("E2E_SIBLING") == "kept", view
    assert view["deployment_keys"] == [_KEY], view

    # Removing the stored copy is accepted; the running value stays the deployment's.
    await api.post("/api/config/env", json={"env": {_KEY: ""}}, retry_on_reloading=True)
    view = await api.get("/api/config/env", retry_on_reloading=True)
    assert _KEY not in view["env"], view
    assert view["deployment_keys"] == [], view
    field = await _schema_field(stack, _KEY)
    assert (field["value_source"], field["value"]) == ("deployment", _DEPLOYMENT_VALUE), field
